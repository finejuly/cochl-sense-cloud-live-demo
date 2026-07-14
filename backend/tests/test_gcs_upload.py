from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.gcs_upload import (
    GcsSessionStillOpenError,
    GcsUploadAuthorizationError,
    UPLOAD_MARKER_FILENAME,
    load_or_create_uploader_id,
    upload_collected_session,
    write_upload_marker,
)


class FakeGoogleError(RuntimeError):
    def __init__(self, code: int):
        super().__init__(f"google error {code}")
        self.code = code


class FakeBlob:
    def __init__(self, name, store, calls, failure_code=None):
        self.name = name
        self.store = store
        self.calls = calls
        self.failure_code = failure_code
        self.metadata = None

    def upload_from_filename(self, filename, **kwargs):
        self._upload(Path(filename).read_bytes(), kwargs)

    def upload_from_string(self, data, **kwargs):
        self._upload(data.encode("utf-8"), kwargs)

    def _upload(self, data, kwargs):
        if self.failure_code:
            raise FakeGoogleError(self.failure_code)
        assert kwargs["if_generation_match"] == 0
        if self.name in self.store:
            raise FakeGoogleError(412)
        self.store[self.name] = {"data": data, "metadata": dict(self.metadata or {})}
        self.calls.append(self.name)


class FakeBucket:
    def __init__(self, store, calls, failure_code=None):
        self.store = store
        self.calls = calls
        self.failure_code = failure_code

    def blob(self, name):
        return FakeBlob(name, self.store, self.calls, self.failure_code)


class FakeStorageClient:
    def __init__(self, failure_code=None):
        self.store = {}
        self.calls = []
        self.failure_code = failure_code
        self.bucket_names = []

    def bucket(self, bucket_name):
        self.bucket_names.append(bucket_name)
        return FakeBucket(self.store, self.calls, self.failure_code)


def make_completed_session(tmp_path, ended_at="2026-07-10T12:01:00+00:00"):
    session_dir = tmp_path / "session-a"
    session_dir.mkdir()
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_name": "Office",
                "started_at": "2026-07-10T12:00:00+00:00",
                "ended_at": ended_at,
            }
        ),
        encoding="utf-8",
    )
    (session_dir / "segment-001-0.000-4.000.mp3").write_bytes(b"mp3-audio")
    (session_dir / "segment-001-0.000-4.000.json").write_text(
        json.dumps({"segment_index": 1, "events": [{"label": "Knock"}]}),
        encoding="utf-8",
    )
    return session_dir


def test_uploads_complete_session_with_manifest_last_and_no_overwrites(tmp_path):
    session_dir = make_completed_session(tmp_path)
    client = FakeStorageClient()

    result = upload_collected_session(
        collected_session_dir=session_dir,
        session_id="session-a",
        bucket_name="test-bucket",
        object_prefix="test-prefix",
        uploader_id="workstation-a",
        storage_client=client,
    )

    assert client.bucket_names == ["test-bucket"]
    assert result.uploaded_file_count == 4
    assert result.existing_file_count == 0
    assert result.object_prefix == (
        f"test-prefix/workstation-a/session-a/{result.snapshot_id}"
    )
    assert client.calls[-1] == f"{result.object_prefix}/manifest.json"
    assert set(client.store) == {
        f"{result.object_prefix}/session.json",
        f"{result.object_prefix}/segments/segment-001-0.000-4.000.mp3",
        f"{result.object_prefix}/segments/segment-001-0.000-4.000.json",
        f"{result.object_prefix}/manifest.json",
    }
    manifest = json.loads(client.store[client.calls[-1]]["data"])
    assert manifest["status"] == "complete"
    assert manifest["snapshot_id"] == result.snapshot_id
    assert len(manifest["files"]) == 3

    repeated = upload_collected_session(
        collected_session_dir=session_dir,
        session_id="session-a",
        bucket_name="test-bucket",
        object_prefix="test-prefix",
        uploader_id="workstation-a",
        storage_client=client,
    )

    assert repeated.snapshot_id == result.snapshot_id
    assert repeated.uploaded_file_count == 0
    assert repeated.existing_file_count == 4

    write_upload_marker(session_dir, result)
    marker = json.loads(
        (session_dir / UPLOAD_MARKER_FILENAME).read_text(encoding="utf-8")
    )
    assert marker["status"] == "uploaded"
    assert marker["snapshot_id"] == result.snapshot_id

    after_marker = upload_collected_session(
        collected_session_dir=session_dir,
        session_id="session-a",
        bucket_name="test-bucket",
        object_prefix="test-prefix",
        uploader_id="workstation-a",
        storage_client=client,
    )
    assert after_marker.snapshot_id == result.snapshot_id
    assert not any(name.endswith(UPLOAD_MARKER_FILENAME) for name in client.store)


def test_reports_file_progress_with_manifest_last(tmp_path):
    session_dir = make_completed_session(tmp_path)
    client = FakeStorageClient()
    progress = []

    upload_collected_session(
        collected_session_dir=session_dir,
        session_id="session-a",
        bucket_name="bucket",
        object_prefix="root",
        uploader_id="uploader-a",
        storage_client=client,
        progress_callback=progress.append,
    )

    assert [event.completed_file_count for event in progress] == [1, 2, 3, 4]
    assert {event.total_file_count for event in progress} == {4}
    assert [event.source_filename for event in progress] == [
        "session.json",
        "segment-001-0.000-4.000.mp3",
        "segment-001-0.000-4.000.json",
        "manifest.json",
    ]
    assert all(event.file_status == "uploaded" for event in progress)
    assert progress[-1].object_name == "manifest.json"


def test_changed_session_content_gets_a_new_snapshot_prefix(tmp_path):
    session_dir = make_completed_session(tmp_path)
    client = FakeStorageClient()
    first = upload_collected_session(
        collected_session_dir=session_dir,
        session_id="session-a",
        bucket_name="bucket",
        object_prefix="root",
        uploader_id="uploader-a",
        storage_client=client,
    )
    (session_dir / "segment-001-0.000-4.000.mp3").write_bytes(b"changed-audio")

    second = upload_collected_session(
        collected_session_dir=session_dir,
        session_id="session-a",
        bucket_name="bucket",
        object_prefix="root",
        uploader_id="uploader-a",
        storage_client=client,
    )

    assert second.snapshot_id != first.snapshot_id
    assert second.object_prefix != first.object_prefix


def test_rejects_open_session(tmp_path):
    session_dir = make_completed_session(tmp_path, ended_at=None)

    with pytest.raises(GcsSessionStillOpenError):
        upload_collected_session(
            collected_session_dir=session_dir,
            session_id="session-a",
            bucket_name="bucket",
            object_prefix="root",
            uploader_id="uploader-a",
            storage_client=FakeStorageClient(),
        )


def test_surfaces_google_permission_failure(tmp_path):
    session_dir = make_completed_session(tmp_path)

    with pytest.raises(GcsUploadAuthorizationError):
        upload_collected_session(
            collected_session_dir=session_dir,
            session_id="session-a",
            bucket_name="bucket",
            object_prefix="root",
            uploader_id="uploader-a",
            storage_client=FakeStorageClient(failure_code=403),
        )


def test_uploader_id_is_generated_once_per_install(tmp_path):
    uploader_id_path = tmp_path / "recordings" / ".gcs-uploader-id"

    first = load_or_create_uploader_id(uploader_id_path)
    second = load_or_create_uploader_id(uploader_id_path)

    assert first.startswith("install-")
    assert second == first
    assert uploader_id_path.read_text(encoding="utf-8").strip() == first
