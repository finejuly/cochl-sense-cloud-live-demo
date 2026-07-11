from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Literal, Protocol
from uuid import uuid4

from backend.app.segment_files import sorted_segment_metadata_paths

GCS_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"
MANIFEST_FILENAME = "manifest.json"
UPLOAD_MARKER_FILENAME = ".gcs-upload.json"
SCHEMA_VERSION = "1.0"
UPLOADER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class GcsUploadConfigurationError(RuntimeError):
    pass


class GcsSessionStillOpenError(RuntimeError):
    pass


class GcsUploadAuthorizationError(RuntimeError):
    pass


class GcsUploadFailedError(RuntimeError):
    pass


class GcsUploadPersistenceError(RuntimeError):
    pass


class StorageBlob(Protocol):
    metadata: dict[str, str] | None

    def upload_from_filename(self, filename: str, **kwargs: Any) -> None: ...

    def upload_from_string(self, data: str, **kwargs: Any) -> None: ...


class StorageBucket(Protocol):
    def blob(self, name: str) -> StorageBlob: ...


class StorageClient(Protocol):
    def bucket(self, bucket_name: str) -> StorageBucket: ...


@dataclass(frozen=True)
class GcsUploadResult:
    object_prefix: str
    snapshot_id: str
    uploaded_file_count: int
    existing_file_count: int
    total_size_bytes: int


@dataclass(frozen=True)
class GcsUploadFileProgress:
    object_name: str
    source_filename: str
    file_status: Literal["uploaded", "existing"]
    completed_file_count: int
    total_file_count: int


def write_upload_marker(
    session_dir: Path,
    result: GcsUploadResult,
    *,
    uploaded_at: datetime | None = None,
) -> None:
    """Persist upload completion without changing the uploaded session snapshot."""
    marker = session_dir / UPLOAD_MARKER_FILENAME
    temporary = session_dir / f"{UPLOAD_MARKER_FILENAME}.{uuid4().hex}.tmp"
    payload = {
        "status": "uploaded",
        "object_prefix": result.object_prefix,
        "snapshot_id": result.snapshot_id,
        "uploaded_at": (uploaded_at or datetime.now(timezone.utc)).isoformat(),
    }
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(marker)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise GcsUploadPersistenceError(
            "Could not persist the local GCS upload status."
        ) from exc


@dataclass(frozen=True)
class _UploadFile:
    path: Path
    source_filename: str
    object_name: str
    size_bytes: int
    sha256: str
    content_type: str


def create_storage_client(project_id: str) -> StorageClient:
    """Create a GCS client from Google Application Default Credentials."""
    try:
        import google.auth
        from google.cloud import storage
    except ImportError as exc:  # pragma: no cover - covered by installation docs
        raise GcsUploadConfigurationError(
            "Google Cloud Storage dependencies are not installed."
        ) from exc

    try:
        credentials, _ = google.auth.default(scopes=[GCS_SCOPE])
        return storage.Client(project=project_id, credentials=credentials)
    except Exception as exc:
        raise GcsUploadAuthorizationError(
            "Google Application Default Credentials are unavailable."
        ) from exc


def load_or_create_uploader_id(path: Path) -> str:
    """Return the stable, collision-resistant id for this app installation."""
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        raise GcsUploadConfigurationError(
            "Could not read the local GCS uploader id."
        ) from exc
    if existing and UPLOADER_ID_PATTERN.fullmatch(existing):
        return existing
    if existing:
        raise GcsUploadConfigurationError("The local GCS uploader id is invalid.")

    generated = f"install-{uuid4()}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as destination:
            destination.write(f"{generated}\n")
        return generated
    except FileExistsError:
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise GcsUploadConfigurationError(
                "Could not read the local GCS uploader id."
            ) from exc
        if existing and UPLOADER_ID_PATTERN.fullmatch(existing):
            return existing
        raise GcsUploadConfigurationError("The local GCS uploader id is invalid.")
    except OSError as exc:
        raise GcsUploadConfigurationError(
            "Could not persist the local GCS uploader id."
        ) from exc


def upload_collected_session(
    *,
    collected_session_dir: Path,
    session_id: str,
    bucket_name: str,
    object_prefix: str,
    uploader_id: str,
    storage_client: StorageClient,
    progress_callback: Callable[[GcsUploadFileProgress], None] | None = None,
) -> GcsUploadResult:
    session_summary = _read_json_object(collected_session_dir / "session.json")
    if not session_summary.get("ended_at"):
        raise GcsSessionStillOpenError(
            "The recording session must end before it can be uploaded."
        )

    with TemporaryDirectory(prefix="cochl-gcs-upload-") as snapshot_name:
        files = _collect_upload_files(
            collected_session_dir,
            Path(snapshot_name),
        )
        return _upload_snapshot(
            files=files,
            session_summary=session_summary,
            session_id=session_id,
            bucket_name=bucket_name,
            object_prefix=object_prefix,
            uploader_id=uploader_id,
            storage_client=storage_client,
            progress_callback=progress_callback,
        )


def _upload_snapshot(
    *,
    files: list[_UploadFile],
    session_summary: dict[str, Any],
    session_id: str,
    bucket_name: str,
    object_prefix: str,
    uploader_id: str,
    storage_client: StorageClient,
    progress_callback: Callable[[GcsUploadFileProgress], None] | None,
) -> GcsUploadResult:
    snapshot_id = _snapshot_id(files)
    base_prefix = "/".join(
        part.strip("/")
        for part in (
            object_prefix,
            uploader_id,
            session_id,
            snapshot_id,
        )
        if part.strip("/")
    )
    bucket = storage_client.bucket(bucket_name)
    uploaded_count = 0
    existing_count = 0
    completed_count = 0
    total_file_count = len(files) + 1

    for upload_file in files:
        blob = bucket.blob(f"{base_prefix}/{upload_file.object_name}")
        blob.metadata = {
            "cochl-session-id": session_id,
            "cochl-uploader-id": uploader_id,
            "cochl-sha256": upload_file.sha256,
            "cochl-snapshot-id": snapshot_id,
        }
        was_uploaded = _upload_filename_once(blob, upload_file)
        if was_uploaded:
            uploaded_count += 1
        else:
            existing_count += 1
        completed_count += 1
        _report_progress(
            progress_callback,
            object_name=upload_file.object_name,
            source_filename=upload_file.source_filename,
            file_status="uploaded" if was_uploaded else "existing",
            completed_file_count=completed_count,
            total_file_count=total_file_count,
        )

    uploaded_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "project_layout": "cochl-sense-live-demo",
        "session_id": session_id,
        "session_name": session_summary.get("session_name"),
        "session_started_at": session_summary.get("started_at"),
        "session_ended_at": session_summary.get("ended_at"),
        "uploader_id": uploader_id,
        "snapshot_id": snapshot_id,
        "uploaded_at": uploaded_at,
        "files": [
            {
                "object_name": upload_file.object_name,
                "source_filename": upload_file.source_filename,
                "content_type": upload_file.content_type,
                "size_bytes": upload_file.size_bytes,
                "sha256": upload_file.sha256,
            }
            for upload_file in files
        ],
    }
    manifest_blob = bucket.blob(f"{base_prefix}/{MANIFEST_FILENAME}")
    manifest_blob.metadata = {
        "cochl-session-id": session_id,
        "cochl-uploader-id": uploader_id,
        "cochl-snapshot-id": snapshot_id,
    }
    manifest_uploaded = _upload_string_once(
        manifest_blob,
        json.dumps(manifest, ensure_ascii=False),
    )
    if manifest_uploaded:
        uploaded_count += 1
    else:
        existing_count += 1
    completed_count += 1
    _report_progress(
        progress_callback,
        object_name=MANIFEST_FILENAME,
        source_filename=MANIFEST_FILENAME,
        file_status="uploaded" if manifest_uploaded else "existing",
        completed_file_count=completed_count,
        total_file_count=total_file_count,
    )

    return GcsUploadResult(
        object_prefix=base_prefix,
        snapshot_id=snapshot_id,
        uploaded_file_count=uploaded_count,
        existing_file_count=existing_count,
        total_size_bytes=sum(upload_file.size_bytes for upload_file in files),
    )


def _report_progress(
    callback: Callable[[GcsUploadFileProgress], None] | None,
    *,
    object_name: str,
    source_filename: str,
    file_status: Literal["uploaded", "existing"],
    completed_file_count: int,
    total_file_count: int,
) -> None:
    if callback is None:
        return
    callback(
        GcsUploadFileProgress(
            object_name=object_name,
            source_filename=source_filename,
            file_status=file_status,
            completed_file_count=completed_file_count,
            total_file_count=total_file_count,
        )
    )


def _collect_upload_files(session_dir: Path, snapshot_dir: Path) -> list[_UploadFile]:
    session_path = session_dir / "session.json"
    if not session_path.is_file():
        raise FileNotFoundError(session_path)

    metadata_paths = sorted_segment_metadata_paths(session_dir)
    if not metadata_paths:
        raise ValueError("The collected session has no segments to upload.")

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stable_session_path = snapshot_dir / "session.json"
    shutil.copyfile(session_path, stable_session_path)
    source_paths: list[tuple[Path, str, str]] = [
        (stable_session_path, "session.json", session_path.name)
    ]
    for metadata_path in metadata_paths:
        stable_audio = _copy_stable_segment_audio(
            session_dir,
            metadata_path.stem,
            snapshot_dir,
        )
        if stable_audio is None:
            raise FileNotFoundError(
                f"Collected audio is missing: {metadata_path.stem}"
            )
        audio_path, source_audio_name = stable_audio
        stable_metadata_path = snapshot_dir / metadata_path.name
        shutil.copyfile(metadata_path, stable_metadata_path)
        source_paths.extend(
            [
                (
                    audio_path,
                    f"segments/{metadata_path.stem}{audio_path.suffix.lower()}",
                    source_audio_name,
                ),
                (
                    stable_metadata_path,
                    f"segments/{metadata_path.stem}.json",
                    metadata_path.name,
                ),
            ]
        )

    files: list[_UploadFile] = []
    for path, object_name, source_filename in source_paths:
        files.append(
            _UploadFile(
                path=path,
                source_filename=source_filename,
                object_name=object_name,
                size_bytes=path.stat().st_size,
                sha256=_sha256_file(path),
                content_type=_content_type(path),
            )
        )
    return files


def _copy_stable_segment_audio(
    session_dir: Path,
    stem: str,
    snapshot_dir: Path,
) -> tuple[Path, str] | None:
    for suffix in (".mp3", ".wav"):
        candidate = session_dir / f"{stem}{suffix}"
        if not candidate.is_file():
            continue
        snapshot_path = snapshot_dir / candidate.name
        try:
            shutil.copyfile(candidate, snapshot_path)
        except FileNotFoundError:
            # Async conversion can replace the WAV with an MP3 between the
            # existence check and the copy; try the sibling extension.
            continue
        return snapshot_path, candidate.name
    return None


def _snapshot_id(files: list[_UploadFile]) -> str:
    digest = hashlib.sha256()
    for upload_file in sorted(files, key=lambda item: item.object_name):
        digest.update(upload_file.object_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(upload_file.sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _content_type(path: Path) -> str:
    if path.suffix.lower() == ".json":
        return "application/json"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _upload_filename_once(blob: StorageBlob, upload_file: _UploadFile) -> bool:
    try:
        blob.upload_from_filename(
            str(upload_file.path),
            content_type=upload_file.content_type,
            if_generation_match=0,
            timeout=120,
        )
        return True
    except Exception as exc:
        return _handle_upload_exception(exc)


def _upload_string_once(blob: StorageBlob, payload: str) -> bool:
    try:
        blob.upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=0,
            timeout=120,
        )
        return True
    except Exception as exc:
        return _handle_upload_exception(exc)


def _handle_upload_exception(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code == 412:
        return False
    if code in {401, 403}:
        raise GcsUploadAuthorizationError(
            "Google credentials do not have permission to upload to this bucket."
        ) from exc
    raise GcsUploadFailedError("Google Cloud Storage upload failed.") from exc


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid collected metadata: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Collected metadata must be a JSON object: {path.name}")
    return payload
