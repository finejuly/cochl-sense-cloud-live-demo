import asyncio
import io
import json
import struct
import threading
import time
import wave
from concurrent.futures import Future
from pathlib import Path

import httpx
import pytest
from anyio import CapacityLimiter
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import (
    LIVE_CONVERSION_MAX_PENDING,
    app,
    convert_live_chunk_to_mp3,
    create_app,
    get_settings,
    schedule_live_chunk_conversion,
)


class FakeProvider:
    def __init__(self, settings):
        self.settings = settings

    def analyze_file(self, path):
        return {
            "sound_event_detection": {
                "status": "success",
                "results": [
                    {
                        "start_time_sec": 0.0,
                        "end_time_sec": 1.0,
                        "classes": [{"class": "Speech", "confidence": 0.9}],
                    }
                ],
            }
        }


def override_settings():
    return Settings(cochl_project_key="test-key")


def override_settings_without_collection():
    return Settings(cochl_project_key="test-key", collection_enabled=False)


def make_wav_bytes(start_sec: float, end_sec: float, framerate: int = 100) -> bytes:
    start_frame = round(start_sec * framerate)
    frame_count = round((end_sec - start_sec) * framerate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(framerate)
        writer.writeframes(
            struct.pack(f"<{frame_count}h", *range(start_frame, start_frame + frame_count))
        )
    return buffer.getvalue()


def test_health_returns_ready():
    client = TestClient(app)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "worker-src 'self' blob:" in response.headers["content-security-policy"]


def test_ready_checks_configuration_and_storage(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", tmp_path / "recordings")
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    client = TestClient(created_app)

    response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert (tmp_path / "recordings").is_dir()


def test_ready_reports_missing_project_key(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", tmp_path / "recordings")
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = lambda: Settings(
        cochl_project_key=None
    )

    response = TestClient(created_app).get("/api/ready")

    assert response.status_code == 503
    assert "COCHL_PROJECT_KEY" in response.json()["detail"]


def test_runtime_config_is_not_cached_and_exposes_local_token():
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = lambda: Settings(
        cochl_project_key="test-key",
        collection_confidence_threshold=0.73,
    )

    response = TestClient(created_app).get("/api/runtime-config")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["collection_confidence_threshold"] == 0.73
    assert response.json()["api_token"] == created_app.state.api_token
    assert set(response.json()["capabilities"]) == {"gcs"}
    assert response.json()["capabilities"]["gcs"] is False


def test_rejects_untrusted_host_and_cross_origin_requests():
    created_app = create_app(frontend_dist=None)
    client = TestClient(created_app)

    bad_host = client.get("/api/health", headers={"host": "attacker.example"})
    bad_origin = client.get(
        "/api/runtime-config", headers={"origin": "https://attacker.example"}
    )
    cross_site_without_origin = client.get(
        "/api/health", headers={"sec-fetch-site": "cross-site"}
    )

    assert bad_host.status_code == 400
    assert bad_origin.status_code == 403
    assert cross_site_without_origin.status_code == 403


def test_browser_writes_require_runtime_config_token(tmp_path, monkeypatch):
    class LiveProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {"sound_event_detection": {"status": "success", "results": []}}

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", tmp_path / "recordings")
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings_without_collection
    created_app.state.provider_factory = lambda settings: LiveProvider(settings)
    client = TestClient(created_app)
    browser_headers = {
        "origin": "http://localhost:5173",
        "sec-fetch-site": "same-site",
    }
    request_kwargs = {
        "data": {
            "session_id": "browser-auth",
            "sequence_id": "1",
            "window_start_sec": "0",
            "window_end_sec": "2",
        },
        "files": {"file": ("chunk.wav", b"wav-audio", "audio/wav")},
    }

    rejected = client.post(
        "/api/analyze-live-chunk", headers=browser_headers, **request_kwargs
    )
    token = client.get("/api/runtime-config", headers=browser_headers).json()["api_token"]
    accepted = client.post(
        "/api/analyze-live-chunk",
        headers={**browser_headers, "x-cochl-local-token": token},
        **request_kwargs,
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 200


def test_live_request_body_is_rejected_before_upload_spooling(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings_without_collection

    response = TestClient(created_app).post(
        "/api/analyze-live-chunk",
        data={
            "session_id": "oversized",
            "sequence_id": "1",
            "window_start_sec": "0",
            "window_end_sec": "2",
        },
        files={"file": ("chunk.wav", b"x" * (2 * 1024 * 1024), "audio/wav")},
    )

    assert response.status_code == 413
    assert not recordings_dir.exists()


def test_chunked_live_request_body_is_limited_while_receiving(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings_without_collection

    boundary = "cochl-streaming-boundary"
    fields = {
        "session_id": "chunked-limit",
        "sequence_id": "1",
        "window_start_sec": "0",
        "window_end_sec": "2",
    }
    multipart_prefix = b"".join(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
        for name, value in fields.items()
    ) + (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="chunk.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()

    async def oversized_chunks():
        yield multipart_prefix
        for _ in range(3):
            yield b"x" * (1024 * 1024)
            await asyncio.sleep(0)
        yield f"\r\n--{boundary}--\r\n".encode()

    async def send_request():
        transport = httpx.ASGITransport(app=created_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
                return await client.post(
                    "/api/analyze-live-chunk",
                    content=oversized_chunks(),
                    headers={
                        "content-type": f"multipart/form-data; boundary={boundary}"
                    },
                )

    response = asyncio.run(send_request())

    assert response.status_code == 413
    assert not recordings_dir.exists()


def test_unknown_api_route_does_not_fall_back_to_spa(tmp_path):
    frontend_dist = tmp_path / "dist"
    frontend_dist.mkdir()
    (frontend_dist / "index.html").write_text("<html>SPA</html>", encoding="utf-8")

    response = TestClient(create_app(frontend_dist=frontend_dist)).get("/api/missing")

    assert response.status_code == 404


def test_completed_session_can_be_uploaded_to_gcs(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    session_dir = recordings_dir / "collected" / "session-gcs"
    session_dir.mkdir(parents=True)
    (session_dir / "segment-001-0.000-4.000.mp3").write_bytes(b"mp3-audio")
    (session_dir / "segment-001-0.000-4.000.json").write_text(
        json.dumps({"segment_index": 1, "events": [{"label": "Knock"}]}),
        encoding="utf-8",
    )
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_name": "Office",
                "started_at": "2026-07-10T12:00:00+00:00",
                "ended_at": "2026-07-10T12:01:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    uploaded = {}

    class Blob:
        metadata = None

        def __init__(self, name):
            self.name = name

        def upload_from_filename(self, filename, **kwargs):
            assert kwargs["if_generation_match"] == 0
            uploaded[self.name] = Path(filename).read_bytes()

        def upload_from_string(self, data, **kwargs):
            assert kwargs["if_generation_match"] == 0
            uploaded[self.name] = data.encode("utf-8")

    class Bucket:
        def blob(self, name):
            return Blob(name)

    class StorageClient:
        def bucket(self, bucket_name):
            assert bucket_name == "test-bucket"
            return Bucket()

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = lambda: Settings(
        cochl_project_key="test-key",
        gcs_project_id="test-project",
        gcs_bucket_name="test-bucket",
        gcs_object_prefix="test-prefix",
        gcs_uploader_id="test-uploader",
    )
    created_app.state.gcs_storage_client_factory = lambda settings: StorageClient()
    client = TestClient(created_app)

    response = client.post("/api/collected-sessions/session-gcs/gcs-upload")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "uploaded"
    assert payload["session_id"] == "session-gcs"
    assert payload["uploaded_file_count"] == 4
    assert payload["object_prefix"].startswith(
        "test-prefix/test-uploader/session-gcs/"
    )
    assert list(uploaded)[-1].endswith("/manifest.json")
    marker = json.loads((session_dir / ".gcs-upload.json").read_text(encoding="utf-8"))
    assert marker["status"] == "uploaded"
    assert marker["snapshot_id"] == payload["snapshot_id"]

    with client.stream(
        "POST",
        "/api/collected-sessions/session-gcs/gcs-upload/progress",
    ) as stream_response:
        stream_events = [json.loads(line) for line in stream_response.iter_lines()]

    assert stream_response.status_code == 200
    assert stream_response.headers["content-type"].startswith("application/x-ndjson")
    progress_events = [event for event in stream_events if event["type"] == "progress"]
    assert [event["completed_file_count"] for event in progress_events] == [1, 2, 3, 4]
    assert progress_events[-1]["source_filename"] == "manifest.json"
    assert stream_events[-1]["type"] == "complete"
    assert stream_events[-1]["session_id"] == "session-gcs"

    restarted_app = create_app(frontend_dist=None)
    restarted_client = TestClient(restarted_app)
    restarted_listing = restarted_client.get("/api/collected-sessions")
    restarted_session = restarted_listing.json()["sessions"][0]
    assert restarted_listing.status_code == 200
    assert restarted_session["session_id"] == "session-gcs"
    assert restarted_session["gcs_upload"]["status"] == "uploaded"
    assert restarted_session["gcs_upload"]["snapshot_id"] == payload["snapshot_id"]


def test_gcs_upload_requires_server_configuration(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    session_dir = recordings_dir / "collected" / "session-gcs"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = lambda: Settings(
        cochl_project_key="test-key",
        gcs_project_id=None,
        gcs_bucket_name=None,
    )
    client = TestClient(created_app)

    response = client.post("/api/collected-sessions/session-gcs/gcs-upload")

    assert response.status_code == 503
    assert "GCS_PROJECT_ID" in response.json()["detail"]


def test_analyze_recording_returns_normalized_result():
    app.dependency_overrides[get_settings] = override_settings
    app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(app)

    response = client.post(
        "/api/analyze-recording",
        files={"file": ("clip.ogg", b"fake-audio", "audio/ogg")},
    )

    app.dependency_overrides.clear()
    app.state.provider_factory = None

    assert response.status_code == 200
    assert response.json()["sound_events"][0]["label"] == "Speech"


def test_empty_uploads_return_bad_request(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", tmp_path / "recordings")
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(created_app)

    recording_response = client.post(
        "/api/analyze-recording",
        files={"file": ("empty.wav", b"", "audio/wav")},
    )
    live_response = client.post(
        "/api/analyze-live-chunk",
        data={
            "session_id": "empty-test",
            "sequence_id": "1",
            "window_start_sec": "0",
            "window_end_sec": "2",
        },
        files={"file": ("empty.wav", b"", "audio/wav")},
    )

    assert recording_response.status_code == 400
    assert live_response.status_code == 400
    assert not any((tmp_path / "recordings").rglob("*.wav"))


def test_recording_provider_does_not_block_other_requests(tmp_path, monkeypatch):
    delay_sec = 0.3

    class SlowRecordingProvider(FakeProvider):
        def analyze_file(self, path):
            time.sleep(delay_sec)
            return {
                "sound_event_detection": {"status": "success", "results": []}
            }

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", tmp_path / "recordings")
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: SlowRecordingProvider(settings)

    async def run_requests():
        transport = httpx.ASGITransport(app=created_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            started_at = time.perf_counter()
            recording_request = asyncio.create_task(
                client.post(
                    "/api/analyze-recording",
                    files={"file": ("clip.wav", b"wav-audio", "audio/wav")},
                )
            )
            await asyncio.sleep(0.02)
            health_response = await client.get("/api/health")
            health_elapsed_sec = time.perf_counter() - started_at
            recording_response = await recording_request
        return health_elapsed_sec, health_response, recording_response

    health_elapsed_sec, health_response, recording_response = asyncio.run(run_requests())

    assert health_response.status_code == 200
    assert health_elapsed_sec < delay_sec / 2
    assert recording_response.status_code == 200


def test_failed_live_analysis_removes_staged_audio_when_collection_is_enabled(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"

    class FailingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            raise RuntimeError("provider failed")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: FailingProvider(settings)
    client = TestClient(created_app)

    response = client.post(
        "/api/analyze-live-chunk",
        data={
            "session_id": "failure-cleanup-test",
            "sequence_id": "1",
            "window_start_sec": "0",
            "window_end_sec": "2",
        },
        files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
    )

    assert response.status_code == 502
    assert not any(recordings_dir.rglob("*.wav"))


def test_live_collection_failure_preserves_analysis_and_removes_staged_audio(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"

    class LiveProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "start_time_sec": 0,
                            "end_time_sec": 2,
                            "classes": [
                                {"class": "Baby_cry", "confidence": 0.91}
                            ],
                        }
                    ],
                }
            }

    async def fail_collection(*args, **kwargs):
        raise OSError("collection storage unavailable")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr("backend.app.main._collect_live_chunk", fail_collection)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: LiveProvider(settings)

    response = TestClient(created_app).post(
        "/api/analyze-live-chunk",
        data={
            "session_id": "collection-failure",
            "sequence_id": "1",
            "window_start_sec": "0",
            "window_end_sec": "2",
        },
        files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
    )

    assert response.status_code == 200
    assert response.json()["sound_events"] == [
        {
            "start_time_sec": 0.0,
            "end_time_sec": 2.0,
            "label": "Baby_cry",
            "confidence": 0.91,
        }
    ]
    assert response.json()["collection_status"] is None
    assert response.json()["curation_progress"] is None
    assert not any(recordings_dir.rglob("*.wav"))


def test_analyze_recording_uses_provider_factory_from_created_app():
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(created_app)

    try:
        response = client.post(
            "/api/analyze-recording",
            files={"file": ("clip.ogg", b"fake-audio", "audio/ogg")},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert response.status_code == 200
    assert response.json()["sound_events"][0]["label"] == "Speech"


def test_analyze_recording_uses_and_removes_unique_conversion_temp(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir()
    existing_wav = recordings_dir / "clip.wav"
    existing_wav.write_bytes(b"existing-user-audio")
    provider_paths = []

    class CapturingProvider(FakeProvider):
        def analyze_file(self, path):
            provider_paths.append(path)
            return super().analyze_file(path)

    def fake_convert_to_wav(input_path, output_path):
        output_path.write_bytes(b"wav-audio")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr("backend.app.audio.convert_to_wav", fake_convert_to_wav)
    app.dependency_overrides[get_settings] = override_settings
    app.state.provider_factory = lambda settings: CapturingProvider(settings)
    client = TestClient(app)

    try:
        response = client.post(
            "/api/analyze-recording",
            files={"file": ("clip.mp3", b"webm-audio", "audio/webm")},
        )
    finally:
        app.dependency_overrides.clear()
        app.state.provider_factory = None

    saved_files = sorted(path.name for path in recordings_dir.iterdir())
    assert response.status_code == 200
    assert saved_files == ["clip.wav", "clip.webm"]
    assert (recordings_dir / "clip.webm").read_bytes() == b"webm-audio"
    assert existing_wav.read_bytes() == b"existing-user-audio"
    assert len(provider_paths) == 1
    assert provider_paths[0].name.startswith(".clip.webm.")
    assert provider_paths[0].name.endswith(".cochl.wav")
    assert not provider_paths[0].exists()


def test_analyze_live_chunk_returns_offset_sound_events_and_preserves_debug_audio(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"
    provider_paths = []
    conversion_calls = []

    class CapturingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            provider_paths.append(path)
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "start_time_sec": 0.25,
                            "end_time_sec": 1.25,
                            "classes": [{"class": "Keyboard", "confidence": 0.72}],
                        }
                    ],
                }
            }

    def fake_convert_live_chunk_to_mp3(wav_path):
        conversion_calls.append(wav_path)
        wav_path.with_suffix(".mp3").write_bytes(b"mp3-audio")
        wav_path.unlink()

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        fake_convert_live_chunk_to_mp3,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings_without_collection
    created_app.state.provider_factory = lambda settings: CapturingProvider(settings)
    client = TestClient(created_app)

    try:
        response = client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "session-abc",
                "sequence_id": "12",
                "window_start_sec": "12.0",
                "window_end_sec": "14.0",
            },
            files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    expected_wav_path = recordings_dir / "live" / "session-abc" / "chunk-000012-12.000-14.000.wav"
    expected_mp3_path = expected_wav_path.with_suffix(".mp3")
    body = response.json()
    assert response.status_code == 200
    assert body["sequence_id"] == 12
    assert body["window_start_sec"] == 12.0
    assert body["window_end_sec"] == 14.0
    assert body["sound_events"] == [
        {
            "start_time_sec": 12.25,
            "end_time_sec": 13.25,
            "label": "Keyboard",
            "confidence": 0.72,
        }
    ]
    assert provider_paths == [expected_wav_path]

    deadline = time.perf_counter() + 1.0
    while time.perf_counter() < deadline and (
        not expected_mp3_path.exists() or expected_wav_path.exists()
    ):
        time.sleep(0.01)

    assert conversion_calls == [expected_wav_path]
    assert expected_mp3_path.read_bytes() == b"mp3-audio"
    assert not expected_wav_path.exists()


def test_analyze_live_chunk_does_not_wait_for_debug_audio_conversion(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"
    conversion_calls = []

    class CapturingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {"sound_event_detection": {"status": "success", "results": []}}

    def slow_convert_live_chunk_to_mp3(wav_path):
        conversion_calls.append(wav_path)
        time.sleep(0.5)
        wav_path.with_suffix(".mp3").write_bytes(b"mp3-audio")
        wav_path.unlink()

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        slow_convert_live_chunk_to_mp3,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings_without_collection
    created_app.state.provider_factory = lambda settings: CapturingProvider(settings)
    client = TestClient(created_app)

    try:
        started_at = time.perf_counter()
        response = client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "conversion-response-test",
                "sequence_id": "1",
                "window_start_sec": "0.0",
                "window_end_sec": "2.0",
            },
            files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
        )
        elapsed_sec = time.perf_counter() - started_at
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    expected_wav_path = (
        recordings_dir
        / "live"
        / "conversion-response-test"
        / "chunk-000001-0.000-2.000.wav"
    )
    expected_mp3_path = expected_wav_path.with_suffix(".mp3")
    assert response.status_code == 200
    assert elapsed_sec < 0.25

    deadline = time.perf_counter() + 1.5
    while time.perf_counter() < deadline and (
        not expected_mp3_path.exists() or expected_wav_path.exists()
    ):
        time.sleep(0.01)

    assert conversion_calls == [expected_wav_path]
    assert expected_mp3_path.read_bytes() == b"mp3-audio"
    assert not expected_wav_path.exists()


def test_live_mp3_conversion_publishes_only_after_conversion_finishes(
    tmp_path, monkeypatch
):
    wav_path = tmp_path / "segment.wav"
    wav_path.write_bytes(b"wav-audio")
    final_mp3_path = wav_path.with_suffix(".mp3")

    def fake_convert(input_path, output_path):
        assert input_path == wav_path
        assert output_path != final_mp3_path
        assert not final_mp3_path.exists()
        output_path.write_bytes(b"complete-mp3")

    monkeypatch.setattr("backend.app.main.convert_to_mp3", fake_convert)

    convert_live_chunk_to_mp3(wav_path)

    assert final_mp3_path.read_bytes() == b"complete-mp3"
    assert not wav_path.exists()
    assert list(tmp_path.glob(".*.tmp.mp3")) == []


def test_segment_conversion_cannot_resurrect_audio_deleted_mid_conversion(
    tmp_path, monkeypatch
):
    from backend.app.collection import delete_collected_segment

    session_dir = tmp_path / "session-a"
    session_dir.mkdir()
    stem = "segment-001-0.000-4.000"
    wav_path = session_dir / f"{stem}.wav"
    wav_path.write_bytes(b"wav-audio")
    (session_dir / f"{stem}.json").write_text("{}", encoding="utf-8")

    def fake_convert(input_path, output_path):
        output_path.write_bytes(b"complete-mp3")
        assert delete_collected_segment(tmp_path, "session-a", wav_path.name)

    monkeypatch.setattr("backend.app.main.convert_to_mp3", fake_convert)

    convert_live_chunk_to_mp3(wav_path)

    assert not (session_dir / f"{stem}.mp3").exists()
    assert not wav_path.exists()
    assert not list(tmp_path.rglob("*.tmp.mp3"))


def test_schedule_live_chunk_conversion_skips_when_backlog_is_full(tmp_path):
    created_app = create_app(frontend_dist=None)

    class FailingExecutor:
        def submit(self, *args, **kwargs):
            raise AssertionError("conversion should not be submitted when backlog is full")

    created_app.state.live_conversion_executor = FailingExecutor()
    created_app.state.live_conversion_futures = {
        Future() for _ in range(LIVE_CONVERSION_MAX_PENDING)
    }

    scheduled = schedule_live_chunk_conversion(created_app, tmp_path / "chunk.wav")

    assert scheduled is False
    assert len(created_app.state.live_conversion_futures) == LIVE_CONVERSION_MAX_PENDING


def test_analyze_live_chunk_rejects_ambiguous_session_id(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    provider_paths = []

    class CapturingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            provider_paths.append(path)
            return {"sound_event_detection": {"status": "success", "results": []}}

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: CapturingProvider(settings)
    client = TestClient(created_app)

    try:
        response = client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "../bad session",
                "sequence_id": "1",
                "window_start_sec": "0.0",
                "window_end_sec": "2.0",
            },
            files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert response.status_code == 422
    assert provider_paths == []
    assert not recordings_dir.exists()


def test_analyze_live_chunk_rejects_invalid_window_metadata(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(created_app)

    response = client.post(
        "/api/analyze-live-chunk",
        data={
            "session_id": "metadata-test",
            "sequence_id": "0",
            "window_start_sec": "2",
            "window_end_sec": "1",
        },
        files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
    )

    assert response.status_code == 422
    assert not recordings_dir.exists()


def test_oversized_upload_is_streamed_and_partial_file_is_removed(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = lambda: Settings(
        cochl_project_key="test-key",
        max_upload_mb=1,
    )
    created_app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(created_app)

    response = client.post(
        "/api/analyze-recording",
        files={"file": ("large.wav", b"x" * (1024 * 1024 + 1), "audio/wav")},
    )

    assert response.status_code == 413
    assert not any(recordings_dir.iterdir())


def test_analyze_live_chunk_handles_concurrent_provider_calls(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    delay_sec = 0.2
    request_count = 4

    class SlowProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            time.sleep(delay_sec)
            return {"sound_event_detection": {"status": "success", "results": []}}

    async def post_chunk(client, sequence_id):
        return await client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "concurrency-test",
                "sequence_id": str(sequence_id),
                "window_start_sec": f"{sequence_id - 1}.0",
                "window_end_sec": f"{sequence_id + 1}.0",
            },
            files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
        )

    async def run_requests():
        transport = httpx.ASGITransport(app=created_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            started_at = time.perf_counter()
            responses = await asyncio.gather(
                *(post_chunk(client, sequence_id) for sequence_id in range(1, request_count + 1))
            )
            return time.perf_counter() - started_at, responses

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: SlowProvider(settings)

    try:
        elapsed_sec, responses = asyncio.run(run_requests())
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert [response.status_code for response in responses] == [200] * request_count
    assert elapsed_sec < delay_sec * 2


def test_analyze_live_chunk_limits_server_side_provider_concurrency(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    delay_sec = 0.1
    request_count = 5
    active_calls = 0
    max_active_calls = 0
    lock = threading.Lock()

    class TrackingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            nonlocal active_calls, max_active_calls
            with lock:
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
            try:
                time.sleep(delay_sec)
                return {"sound_event_detection": {"status": "success", "results": []}}
            finally:
                with lock:
                    active_calls -= 1

    async def post_chunk(client, sequence_id):
        return await client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "concurrency-limit-test",
                "sequence_id": str(sequence_id),
                "window_start_sec": f"{sequence_id - 1}.0",
                "window_end_sec": f"{sequence_id + 1}.0",
            },
            files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
        )

    async def run_requests():
        transport = httpx.ASGITransport(app=created_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await asyncio.gather(
                *(post_chunk(client, sequence_id) for sequence_id in range(1, request_count + 1))
            )

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: TrackingProvider(settings)
    created_app.state.live_provider_limiter = CapacityLimiter(2)

    try:
        responses = asyncio.run(run_requests())
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert [response.status_code for response in responses] == [200] * request_count
    assert max_active_calls == 2


def test_live_provider_deadline_returns_gateway_timeout(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"

    class HangingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            time.sleep(0.15)
            return {"sound_event_detection": {"status": "success", "results": []}}

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = lambda: Settings(
        cochl_project_key="test-key",
        collection_enabled=False,
        cochl_live_timeout_sec=0.02,
    )
    created_app.state.provider_factory = lambda settings: HangingProvider(settings)

    started_at = time.perf_counter()
    response = TestClient(created_app).post(
        "/api/analyze-live-chunk",
        data={
            "session_id": "deadline",
            "sequence_id": "1",
            "window_start_sec": "0",
            "window_end_sec": "2",
        },
        files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
    )
    elapsed = time.perf_counter() - started_at

    assert response.status_code == 504
    assert "deadline" in response.json()["detail"]
    assert elapsed < 0.12


def test_live_provider_deadline_includes_capacity_queue_wait(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    provider_started = threading.Event()
    class HangingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            provider_started.set()
            time.sleep(0.4)
            return {"sound_event_detection": {"status": "success", "results": []}}

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = lambda: Settings(
        cochl_project_key="test-key",
        collection_enabled=False,
        cochl_live_timeout_sec=0.1,
    )
    created_app.state.provider_factory = lambda settings: HangingProvider(settings)
    created_app.state.live_provider_limiter = CapacityLimiter(1)

    async def post_chunk(client, sequence_id):
        return await client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "queued-deadline",
                "sequence_id": str(sequence_id),
                "window_start_sec": str(sequence_id - 1),
                "window_end_sec": str(sequence_id + 1),
            },
            files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
        )

    async def run_requests():
        transport = httpx.ASGITransport(app=created_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            first = asyncio.create_task(post_chunk(client, 1))
            assert await asyncio.to_thread(provider_started.wait, 1)
            second_started_at = time.perf_counter()
            second = asyncio.create_task(post_chunk(client, 2))
            responses = await asyncio.gather(first, second)
            return responses, time.perf_counter() - second_started_at

    responses, second_elapsed = asyncio.run(run_requests())

    assert [response.status_code for response in responses] == [504, 504]
    assert second_elapsed < 0.16


def post_live_chunk(client, session_id, sequence_id, start_sec, end_sec):
    return client.post(
        "/api/analyze-live-chunk",
        data={
            "session_id": session_id,
            "sequence_id": str(sequence_id),
            "window_start_sec": f"{start_sec:.1f}",
            "window_end_sec": f"{end_sec:.1f}",
        },
        files={
            "file": ("chunk.wav", make_wav_bytes(start_sec, end_sec), "audio/wav"),
        },
    )


def test_analyze_live_chunk_collects_meaningful_audio_into_segments(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"

    class DetectingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "start_time_sec": 0.25,
                            "end_time_sec": 1.25,
                            "classes": [{"class": "Keyboard", "confidence": 0.72}],
                        }
                    ],
                }
            }

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: DetectingProvider(settings)
    client = TestClient(created_app)

    try:
        responses = [
            post_live_chunk(client, "collect-test", sequence_id, start, end)
            for sequence_id, (start, end) in enumerate(
                [(0, 2), (1, 3), (2, 4), (3, 5)], start=1
            )
        ]
        end_response = client.post(
            "/api/live-session/end",
            data={"session_id": "collect-test"},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert [response.status_code for response in responses] == [200] * 4
    assert [response.json()["collection_status"] for response in responses] == [
        "collected",
        "collected",
        "collected",
        "collected",
    ]

    summary = end_response.json()
    assert end_response.status_code == 200
    assert summary["session_id"] == "collect-test"
    assert summary["segment_count"] == 1
    assert summary["kept_chunk_count"] == 4
    assert summary["total_collected_duration_sec"] == 5.0
    segment = summary["segments"][0]
    assert segment["labels"] == ["Keyboard"]

    collected_dir = recordings_dir / "collected" / "collect-test"
    assert (collected_dir / segment["audio_filename"]).exists()
    metadata = json.loads(
        (collected_dir / segment["metadata_filename"]).read_text("utf-8")
    )
    assert metadata["chunk_sequence_ids"] == [1, 2, 3, 4]
    assert not (recordings_dir / "live" / "collect-test").exists()


def test_live_chunk_response_reports_curation_progress_before_session_end(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"

    class SparseDetectingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            sequence_id = int(path.name.split("-")[1])
            results = []
            if sequence_id in {1, 9}:
                results = [
                    {
                        "start_time_sec": 0.0,
                        "end_time_sec": 1.0,
                        "classes": [{"class": "Knock", "confidence": 0.9}],
                    }
                ]
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": results,
                }
            }

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: SparseDetectingProvider(
        settings
    )
    client = TestClient(created_app)

    try:
        responses = [
            post_live_chunk(
                client,
                "progress-test",
                sequence_id,
                sequence_id - 1,
                sequence_id + 1,
            )
            for sequence_id in range(1, 17)
        ]
        progress_after_first_decision = responses[7].json()["curation_progress"]
        progress_after_second_decision = responses[15].json()["curation_progress"]
        end_response = client.post(
            "/api/live-session/end",
            data={"session_id": "progress-test"},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert progress_after_first_decision == {
        "candidate_segment_count": 1,
        "selected_segment_count": 1,
        "rejected_repetitive_count": 0,
        "rejected_class_balance_count": 0,
        "rejected_session_budget_count": 0,
        "invalid_audio_count": 0,
        "write_error_count": 0,
    }
    assert progress_after_second_decision == {
        "candidate_segment_count": 2,
        "selected_segment_count": 1,
        "rejected_repetitive_count": 1,
        "rejected_class_balance_count": 0,
        "rejected_session_budget_count": 0,
        "invalid_audio_count": 0,
        "write_error_count": 0,
    }
    summary = end_response.json()
    assert summary["candidate_segment_count"] == 2
    assert summary["segment_count"] == 1
    assert summary["rejected_repetitive_count"] == 1


def test_analyze_live_chunk_discards_speech_chunks_for_privacy(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(created_app)

    class SpeechProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "start_time_sec": 0.0,
                            "end_time_sec": 1.0,
                            "classes": [{"class": "Male_speech", "confidence": 0.9}],
                        }
                    ],
                }
            }

    created_app.state.provider_factory = lambda settings: SpeechProvider(settings)

    try:
        chunk_response = post_live_chunk(client, "speech-test", 1, 0, 2)
        end_response = client.post(
            "/api/live-session/end",
            data={"session_id": "speech-test"},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert chunk_response.status_code == 200
    assert chunk_response.json()["collection_status"] == "discarded_speech"
    summary = end_response.json()
    assert summary["segment_count"] == 0
    assert summary["discarded_speech_chunk_count"] == 1
    assert not (recordings_dir / "live" / "speech-test").exists()
    empty_session_dir = recordings_dir / "collected" / "speech-test"
    assert (empty_session_dir / ".session-closed.json").is_file()
    assert not (empty_session_dir / "session.json").exists()
    assert client.get("/api/collected-sessions").json()["sessions"] == []


def test_live_session_records_name_and_timestamps(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"

    class DetectingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "start_time_sec": 0.0,
                            "end_time_sec": 1.0,
                            "classes": [{"class": "Knock", "confidence": 0.8}],
                        }
                    ],
                }
            }

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: DetectingProvider(settings)
    client = TestClient(created_app)

    try:
        chunk_response = client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "named-test",
                "sequence_id": "1",
                "window_start_sec": "0.0",
                "window_end_sec": "2.0",
                "session_name": "  사무실 소음  ",
            },
            files={"file": ("chunk.wav", make_wav_bytes(0, 2), "audio/wav")},
        )
        for sequence_id, (start_sec, end_sec) in enumerate(
            [(1, 3), (2, 4), (3, 5)],
            start=2,
        ):
            post_live_chunk(
                client,
                "named-test",
                sequence_id,
                start_sec,
                end_sec,
            )
        end_response = client.post(
            "/api/live-session/end",
            data={"session_id": "named-test", "session_name": "사무실 소음"},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert chunk_response.status_code == 200
    summary = end_response.json()
    assert summary["session_name"] == "사무실 소음"
    assert summary["started_at"] is not None
    assert summary["ended_at"] is not None
    session_json = json.loads(
        (recordings_dir / "collected" / "named-test" / "session.json").read_text("utf-8")
    )
    assert session_json["session_name"] == "사무실 소음"


def test_collected_sessions_can_be_listed_served_and_deleted(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"

    class DetectingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "start_time_sec": 0.0,
                            "end_time_sec": 1.0,
                            "classes": [{"class": "Knock", "confidence": 0.8}],
                        }
                    ],
                }
            }

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: DetectingProvider(settings)
    client = TestClient(created_app)

    try:
        for sequence_id, (start_sec, end_sec) in enumerate(
            [(0, 2), (1, 3), (2, 4), (3, 5)],
            start=1,
        ):
            post_live_chunk(
                client,
                "manage-test",
                sequence_id,
                start_sec,
                end_sec,
            )
        client.post(
            "/api/live-session/end",
            data={"session_id": "manage-test", "session_name": "관리 테스트"},
        )

        listing = client.get("/api/collected-sessions")
        sessions = listing.json()["sessions"]
        assert listing.status_code == 200
        assert len(sessions) == 1
        session = sessions[0]
        assert session["session_id"] == "manage-test"
        assert session["session_name"] == "관리 테스트"
        assert session["candidate_segment_count"] == 1
        assert session["policy_selected_segment_count"] == 1
        assert session["rejected_repetitive_count"] == 0
        segment = session["segments"][0]

        audio_response = client.get(
            f"/api/collected-sessions/manage-test/files/{segment['audio_filename']}"
        )
        assert audio_response.status_code == 200
        assert audio_response.headers["content-type"].startswith("audio/")

        traversal_response = client.get(
            "/api/collected-sessions/..%2F..%2Fmanage-test/files/secret.wav"
        )
        assert traversal_response.status_code == 404

        segment_delete = client.delete(
            f"/api/collected-sessions/manage-test/segments/{segment['audio_filename']}"
        )
        assert segment_delete.status_code == 200
        remaining = client.get("/api/collected-sessions").json()["sessions"]
        assert len(remaining) == 1
        assert remaining[0]["segment_count"] == 0

        session_delete = client.delete("/api/collected-sessions/manage-test")
        assert session_delete.status_code == 200
        assert client.get("/api/collected-sessions").json()["sessions"] == []
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None


def test_collected_file_falls_back_to_converted_extension(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    session_dir = recordings_dir / "collected" / "session-a"
    session_dir.mkdir(parents=True)
    # The WAV was already replaced by the async MP3 conversion.
    (session_dir / "segment-001-0.000-4.000.mp3").write_bytes(b"mp3-audio")
    (session_dir / "segment-001-0.000-4.000.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    client = TestClient(created_app)

    stale_wav = client.get(
        "/api/collected-sessions/session-a/files/segment-001-0.000-4.000.wav"
    )
    metadata = client.get(
        "/api/collected-sessions/session-a/files/segment-001-0.000-4.000.json"
    )
    missing = client.get(
        "/api/collected-sessions/session-a/files/segment-999-0.000-4.000.wav"
    )

    assert stale_wav.status_code == 200
    assert stale_wav.headers["content-type"].startswith("audio/mpeg")
    assert stale_wav.content == b"mp3-audio"
    assert metadata.status_code == 200
    assert missing.status_code == 404


def test_collected_file_never_serves_orphaned_audio_or_metadata(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    session_dir = recordings_dir / "collected" / "session-a"
    session_dir.mkdir(parents=True)
    (session_dir / "segment-001-0.000-4.000.mp3").write_bytes(b"orphan-audio")
    (session_dir / "segment-002-4.000-8.000.json").write_text(
        "{}", encoding="utf-8"
    )

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    client = TestClient(create_app(frontend_dist=None))

    audio = client.get(
        "/api/collected-sessions/session-a/files/segment-001-0.000-4.000.mp3"
    )
    metadata = client.get(
        "/api/collected-sessions/session-a/files/segment-002-4.000-8.000.json"
    )

    assert audio.status_code == 404
    assert metadata.status_code == 404


def test_delete_collected_session_endpoint_removes_directory(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    session_dir = recordings_dir / "collected" / "session-a"
    session_dir.mkdir(parents=True)
    (session_dir / "segment-001-0.000-2.000.wav").write_bytes(b"wav")
    (session_dir / "segment-001-0.000-2.000.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    client = TestClient(created_app)

    response = client.delete("/api/collected-sessions/session-a")

    assert response.status_code == 200
    assert not session_dir.exists()


def test_delete_endpoints_reject_open_collected_session(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    session_dir = recordings_dir / "collected" / "session-a"
    session_dir.mkdir(parents=True)
    audio_name = "segment-001-0.000-2.000.wav"
    (session_dir / audio_name).write_bytes(b"wav")
    (session_dir / "segment-001-0.000-2.000.json").write_text(
        "{}", encoding="utf-8"
    )
    (session_dir / "session.json").write_text(
        json.dumps({"session_id": "session-a", "ended_at": None}),
        encoding="utf-8",
    )

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    client = TestClient(create_app(frontend_dist=None))

    session_response = client.delete("/api/collected-sessions/session-a")
    segment_response = client.delete(
        f"/api/collected-sessions/session-a/segments/{audio_name}"
    )

    assert session_response.status_code == 409
    assert segment_response.status_code == 409
    assert "종료 후 삭제" in session_response.json()["detail"]
    assert session_dir.exists()
    assert (session_dir / audio_name).exists()


def test_delete_endpoint_rejects_non_object_session_summary(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    session_dir = recordings_dir / "collected" / "session-a"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    response = TestClient(create_app(frontend_dist=None)).delete(
        "/api/collected-sessions/session-a"
    )

    assert response.status_code == 409
    assert session_dir.exists()


def test_delete_collected_segment_succeeds_after_session_ends(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    session_dir = recordings_dir / "collected" / "session-a"
    session_dir.mkdir(parents=True)
    audio_name = "segment-001-0.000-2.000.wav"
    (session_dir / audio_name).write_bytes(b"wav")
    (session_dir / "segment-001-0.000-2.000.json").write_text(
        "{}", encoding="utf-8"
    )
    (session_dir / "session.json").write_text(
        json.dumps(
            {"session_id": "session-a", "ended_at": "2026-07-11T00:00:00Z"}
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    client = TestClient(create_app(frontend_dist=None))

    response = client.delete(
        f"/api/collected-sessions/session-a/segments/{audio_name}"
    )

    assert response.status_code == 200
    assert not (session_dir / audio_name).exists()


def test_startup_removes_orphaned_live_chunks_when_collection_enabled(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"
    orphan = recordings_dir / "live" / "old-session" / "chunk-000001.wav"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"wav")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr("backend.app.main.get_settings", override_settings)
    created_app = create_app(frontend_dist=None)

    with TestClient(created_app):
        pass

    assert not (recordings_dir / "live").exists()


def test_startup_keeps_live_chunks_when_collection_disabled(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    orphan = recordings_dir / "live" / "old-session" / "chunk-000001.wav"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"wav")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.get_settings",
        override_settings_without_collection,
    )
    created_app = create_app(frontend_dist=None)

    with TestClient(created_app):
        pass

    assert orphan.exists()


def test_only_one_server_can_use_a_recordings_directory(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    first_app = create_app(frontend_dist=None)
    second_app = create_app(frontend_dist=None)
    first_app.dependency_overrides[get_settings] = override_settings
    second_app.dependency_overrides[get_settings] = override_settings

    with TestClient(first_app):
        with pytest.raises(RuntimeError, match="already using this recordings directory"):
            with TestClient(second_app):
                pass

    replacement_app = create_app(frontend_dist=None)
    replacement_app.dependency_overrides[get_settings] = override_settings
    with TestClient(replacement_app) as client:
        assert client.get("/api/ready").status_code == 200


def test_lifespan_honors_dependency_overridden_settings(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    orphan = recordings_dir / "live" / "old-session" / "chunk-000001.wav"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"wav")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setenv("COCHL_PROJECT_KEY", "environment-key")
    monkeypatch.setenv("COCHL_COLLECTION_ENABLED", "true")
    get_settings.cache_clear()
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings_without_collection
    try:
        with TestClient(created_app):
            pass
    finally:
        get_settings.cache_clear()

    assert orphan.exists()


def test_startup_removes_only_known_conversion_temp_files(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    session_dir = recordings_dir / "collected" / "session-a"
    live_dir = recordings_dir / "live" / "debug-session"
    session_dir.mkdir(parents=True)
    live_dir.mkdir(parents=True)
    provider_temp = recordings_dir / (
        ".clip.webm.0123456789abcdef0123456789abcdef.cochl.wav"
    )
    live_provider_temp = live_dir / (
        ".chunk-000001.wav.0123456789abcdef0123456789abcdef.cochl.ogg"
    )
    segment_temp = session_dir / ".segment-001-0.000-2.000.tmp.mp3"
    normal_audio = session_dir / "segment-001-0.000-2.000.wav"
    normal_metadata = session_dir / "segment-001-0.000-2.000.json"
    unrelated_hidden = session_dir / ".notes.tmp.mp3"
    user_named_wav = recordings_dir / ".user.cochl.wav"
    live_conversion_temp = live_dir / ".chunk-000001-0.000-2.000.tmp.mp3"
    live_debug_wav = live_dir / "chunk-000001-0.000-2.000.wav"
    atomic_wav_temp = session_dir / ".segment-002-2.000-4.000.wav.tmp"
    atomic_metadata_temp = session_dir / ".segment-002-2.000-4.000.json.tmp"
    session_summary_temp = session_dir / ".session.recovery.json.tmp"
    orphan_audio = session_dir / "segment-003-4.000-6.000.wav"
    orphan_metadata = session_dir / "segment-004-6.000-8.000.json"
    for path in (
        provider_temp,
        live_provider_temp,
        segment_temp,
        normal_audio,
        normal_metadata,
        unrelated_hidden,
        user_named_wav,
        live_conversion_temp,
        live_debug_wav,
        atomic_wav_temp,
        atomic_metadata_temp,
        session_summary_temp,
        orphan_audio,
        orphan_metadata,
    ):
        path.write_bytes(b"data")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings_without_collection

    with TestClient(created_app):
        pass

    assert not provider_temp.exists()
    assert not live_provider_temp.exists()
    assert not segment_temp.exists()
    assert not live_conversion_temp.exists()
    assert not atomic_wav_temp.exists()
    assert not atomic_metadata_temp.exists()
    assert not session_summary_temp.exists()
    assert not orphan_audio.exists()
    assert not orphan_metadata.exists()
    assert normal_audio.exists()
    assert normal_metadata.exists()
    assert unrelated_hidden.exists()
    assert user_named_wav.exists()
    assert live_debug_wav.exists()


def test_shutdown_closes_executors_even_if_session_finalization_fails(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", tmp_path / "recordings")
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    shutdown_calls = []

    class BrokenManager:
        @staticmethod
        def recover_incomplete_sessions():
            return []

        @staticmethod
        def end_all_sessions():
            raise OSError("terminal summary write failed")

    class TrackingExecutor:
        def __init__(self, name):
            self.name = name

        def shutdown(self, *, wait, cancel_futures):
            shutdown_calls.append((self.name, wait, cancel_futures))

    created_app.state.live_collection_manager = BrokenManager()
    for name in (
        "live_conversion_executor",
        "live_provider_executor",
        "recording_provider_executor",
    ):
        setattr(created_app.state, name, TrackingExecutor(name))

    with pytest.raises(OSError, match="terminal summary write failed"):
        with TestClient(created_app) as client:
            assert client.get("/api/health").status_code == 200

    assert shutdown_calls == [
        ("live_conversion_executor", False, True),
        ("live_provider_executor", False, True),
        ("recording_provider_executor", False, True),
    ]


def test_end_live_session_for_unknown_session_returns_empty_summary():
    created_app = create_app(frontend_dist=None)
    client = TestClient(created_app)

    response = client.post(
        "/api/live-session/end",
        data={"session_id": "missing-session"},
    )

    summary = response.json()
    assert response.status_code == 200
    assert summary["session_id"] == "missing-session"
    assert summary["segment_count"] == 0
    assert summary["kept_chunk_count"] == 0
    assert summary["segments"] == []


def test_end_live_session_reports_durable_finalization_failure():
    created_app = create_app(frontend_dist=None)

    class BrokenManager:
        @staticmethod
        def end_session(*args, **kwargs):
            raise OSError("disk unavailable")

    created_app.state.live_collection_manager = BrokenManager()

    response = TestClient(created_app).post(
        "/api/live-session/end",
        data={"session_id": "persist-failure"},
    )

    assert response.status_code == 500
    assert "durably finalize" in response.json()["detail"]


def test_late_chunk_is_reported_as_discarded_after_session_end(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"

    class EmptyProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {"sound_event_detection": {"status": "success", "results": []}}

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: EmptyProvider(settings)
    client = TestClient(created_app)

    end_response = client.post(
        "/api/live-session/end",
        data={"session_id": "late-status-test"},
    )
    chunk_response = client.post(
        "/api/analyze-live-chunk",
        data={
            "session_id": "late-status-test",
            "sequence_id": "1",
            "window_start_sec": "0",
            "window_end_sec": "2",
        },
        files={"file": ("chunk.wav", make_wav_bytes(0, 2), "audio/wav")},
    )

    assert end_response.status_code == 200
    assert chunk_response.status_code == 200
    assert chunk_response.json()["collection_status"] == "discarded_late"
    assert not any(recordings_dir.rglob("*.wav"))


def test_end_live_session_rejects_invalid_session_id():
    client = TestClient(create_app(frontend_dist=None))

    response = client.post(
        "/api/live-session/end",
        data={"session_id": "../ambiguous"},
    )

    assert response.status_code == 422


def test_create_app_serves_built_frontend(tmp_path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>Cochl.Sense Cloud Live Demo</title>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")
    client = TestClient(create_app(frontend_dist=dist))

    index_response = client.get("/")
    asset_response = client.get("/assets/app.js")

    assert index_response.status_code == 200
    assert "Cochl.Sense Cloud Live Demo" in index_response.text
    assert asset_response.status_code == 200
    assert "console.log" in asset_response.text


def test_create_app_does_not_serve_files_outside_frontend_dist(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><title>Cochl.Sense Cloud Live Demo</title>",
        encoding="utf-8",
    )
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    client = TestClient(create_app(frontend_dist=dist))

    response = client.get("/%2e%2e/secret.txt")

    assert response.status_code == 200
    assert "Cochl.Sense Cloud Live Demo" in response.text
    assert "secret" not in response.text
