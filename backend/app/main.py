from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
from collections.abc import AsyncIterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import lru_cache
from itertools import count
from math import isfinite
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import BinaryIO

try:  # Unix/macOS runtime used by the app and CI.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback is exercised there.
    fcntl = None

try:  # Keep standalone Windows backend use fail-closed as well.
    import msvcrt
except ImportError:  # pragma: no cover - unavailable on Unix/macOS.
    msvcrt = None

from anyio import CapacityLimiter
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.app.audio import (
    AudioConversionError,
    DuplicateUploadError,
    EmptyUploadError,
    UploadTooLargeError,
    convert_to_mp3,
    extension_for_content_type,
    prepare_audio_for_cochl,
    validate_upload_size,
)
from backend.app.cochl_provider import CochlProvider, CochlProviderTimeoutError
from backend.app.collection import (
    LiveCollectionManager,
    delete_collected_segment,
    delete_collected_session,
    list_collected_sessions,
    policy_from_settings,
    publish_segment_conversion,
    safe_collected_session_dir,
)
from backend.app.config import Settings
from backend.app.gcs_upload import (
    GcsSessionStillOpenError,
    GcsUploadAuthorizationError,
    GcsUploadConfigurationError,
    GcsUploadFailedError,
    GcsUploadFileProgress,
    GcsUploadPersistenceError,
    StorageClient,
    create_storage_client,
    load_or_create_uploader_id,
    upload_collected_session,
    write_upload_marker,
)
from backend.app.http_safety import (
    LocalAccessMiddleware,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
)
from backend.app.models import (
    AnalysisResponse,
    CollectedSessionsResponse,
    DeletionResponse,
    GcsSessionUploadResponse,
    LiveChunkAnalysisResponse,
    LiveCurationProgress,
    LiveSessionEndResponse,
    ReadinessResponse,
    RuntimeCapabilities,
    RuntimeConfigResponse,
    SoundEvent,
)
from backend.app.normalization import (
    CochlContractError,
    normalize_cochl_result,
    normalize_sound_events,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
DEFAULT_RECORDINGS_DIR = PROJECT_ROOT / "recordings"
LIVE_PROVIDER_MAX_CONCURRENCY = 10
RECORDING_PROVIDER_MAX_CONCURRENCY = 2
LIVE_CONVERSION_MAX_WORKERS = 2
LIVE_CONVERSION_MAX_PENDING = 32
DEFAULT_LIVE_PROVIDER_TIMEOUT_SEC = 20.0
DEFAULT_RECORDING_PROVIDER_TIMEOUT_SEC = 900.0
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
LIVE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PROVIDER_CONVERSION_TEMP_PATTERN = re.compile(
    r"^\..+\.[0-9a-f]{32}\.cochl\.(?:ogg|wav)$"
)
INSTANCE_LOCK_FILENAME = ".cochl-sense-cloud-live-demo.lock"
logger = logging.getLogger(__name__)
_gcs_progress_upload_tasks: set[asyncio.Task[None]] = set()


class ProviderBusyError(RuntimeError):
    pass


class ProviderTimeoutError(TimeoutError):
    pass


@asynccontextmanager
async def lifespan(current_app: FastAPI):
    # Use the same resolver as request handling so embedded deployments and
    # tests do not start with one configuration and serve with another.
    settings = _settings_for_app(current_app)
    settings.validate_service_combination()
    settings.validate_timeouts()
    settings.validate_upload()
    settings.validate_collection()
    settings.validate_gcs()
    try:
        _verify_recordings_storage()
    except OSError as exc:
        raise RuntimeError(
            f"Recordings directory is not writable: {DEFAULT_RECORDINGS_DIR}"
        ) from exc
    _acquire_instance_lock(current_app)
    try:
        manager: LiveCollectionManager = current_app.state.live_collection_manager
        await run_in_threadpool(manager.recover_incomplete_sessions)
        await run_in_threadpool(cleanup_orphan_conversion_temps)
        await run_in_threadpool(cleanup_orphan_live_chunks, settings)
        try:
            yield
        finally:
            shutdown_error: Exception | None = None
            try:
                await run_in_threadpool(manager.end_all_sessions)
            except Exception as exc:
                shutdown_error = exc
                logger.exception("Could not finalize every live session during shutdown.")
            if shutdown_error is not None:
                raise shutdown_error
    finally:
        try:
            # Executor cleanup must not depend on startup/session persistence
            # succeeding.
            for executor_name in (
                "live_conversion_executor",
                "live_provider_executor",
                "recording_provider_executor",
            ):
                executor = getattr(current_app.state, executor_name, None)
                if executor is not None:
                    executor.shutdown(wait=False, cancel_futures=True)
        finally:
            _release_instance_lock(current_app)


def _acquire_instance_lock(current_app: FastAPI) -> None:
    lock_path = DEFAULT_RECORDINGS_DIR / INSTANCE_LOCK_FILENAME
    lock_file = lock_path.open("a+b")
    try:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:  # pragma: no cover - Windows-only branch.
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - no supported local locking primitive.
            raise RuntimeError("This platform does not provide a file-locking primitive.")
    except (BlockingIOError, OSError) as exc:
        lock_file.close()
        raise RuntimeError(
            "Another Cochl.Sense Cloud Live Demo server is already using this recordings directory."
        ) from exc
    current_app.state.instance_lock_file = lock_file


def _release_instance_lock(current_app: FastAPI) -> None:
    lock_file = getattr(current_app.state, "instance_lock_file", None)
    if lock_file is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows-only branch.
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        lock_file.close()
        current_app.state.instance_lock_file = None


def cleanup_orphan_live_chunks(settings: Settings | None = None) -> None:
    """Removes live chunk staging left behind by a previous process.

    With collection enabled, `recordings/live/` only holds chunks awaiting
    classification; anything there at startup is an orphan from a crashed or
    restarted server. With collection disabled, live chunks are intentional
    debug output and must be kept.
    """
    settings = settings or get_settings()
    if not settings.collection_enabled:
        return
    live_root = DEFAULT_RECORDINGS_DIR / "live"
    if not live_root.is_dir():
        return
    try:
        shutil.rmtree(live_root)
    except OSError:
        logger.exception("Could not clean orphaned live chunk directory %s.", live_root)
    else:
        logger.info("Removed orphaned live chunk staging directory %s.", live_root)


def cleanup_orphan_conversion_temps() -> None:
    """Remove only scratch files whose names are owned by conversion helpers."""

    candidates = {
        path
        for path in DEFAULT_RECORDINGS_DIR.glob(".*.cochl.*")
        if PROVIDER_CONVERSION_TEMP_PATTERN.fullmatch(path.name)
    }
    collected_root = _collected_root()
    if collected_root.is_dir():
        for session_dir in collected_root.iterdir():
            if not session_dir.is_dir():
                continue
            for pattern in (
                ".segment-*.tmp.mp3",
                ".segment-*.wav.tmp",
                ".segment-*.json.tmp",
                ".session*.tmp",
            ):
                candidates.update(session_dir.glob(pattern))

            # Atomic segment publication makes metadata the visibility marker.
            # At process start there is no active publisher, so an audio file
            # without final metadata (or vice versa) is safe to discard.
            for audio_path in (
                *session_dir.glob("segment-*.wav"),
                *session_dir.glob("segment-*.mp3"),
            ):
                if not audio_path.with_suffix(".json").is_file():
                    candidates.add(audio_path)
            for metadata_path in session_dir.glob("segment-*.json"):
                stem = metadata_path.stem
                if not any(
                    (session_dir / f"{stem}{suffix}").is_file()
                    for suffix in (".wav", ".mp3")
                ):
                    candidates.add(metadata_path)

    live_root = DEFAULT_RECORDINGS_DIR / "live"
    if live_root.is_dir():
        candidates.update(live_root.glob("*/.chunk-*.tmp.mp3"))
        candidates.update(
            path
            for path in live_root.glob("*/.*.cochl.*")
            if PROVIDER_CONVERSION_TEMP_PATTERN.fullmatch(path.name)
        )
    for candidate in candidates:
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove orphaned conversion temp %s.", candidate)


def create_app(frontend_dist: Path | None = DEFAULT_FRONTEND_DIST) -> FastAPI:
    created_app = FastAPI(
        title="Cochl.Sense Cloud Live Demo API",
        version="0.1.0",
        lifespan=lifespan,
    )
    created_app.state.api_token = secrets.token_urlsafe(32)
    created_app.state.provider_factory = None
    created_app.state.gcs_storage_client_factory = None
    created_app.state.live_provider_limiter = CapacityLimiter(LIVE_PROVIDER_MAX_CONCURRENCY)
    created_app.state.recording_provider_limiter = CapacityLimiter(
        RECORDING_PROVIDER_MAX_CONCURRENCY
    )
    created_app.state.live_provider_executor = ThreadPoolExecutor(
        max_workers=LIVE_PROVIDER_MAX_CONCURRENCY,
        thread_name_prefix="cochl-sense-cloud-live-provider",
    )
    created_app.state.live_provider_futures = set()
    created_app.state.live_provider_lock = Lock()
    created_app.state.recording_provider_executor = ThreadPoolExecutor(
        max_workers=RECORDING_PROVIDER_MAX_CONCURRENCY,
        thread_name_prefix="cochl-sense-cloud-recording-provider",
    )
    created_app.state.recording_provider_futures = set()
    created_app.state.recording_provider_lock = Lock()
    created_app.state.live_conversion_executor = ThreadPoolExecutor(
        max_workers=LIVE_CONVERSION_MAX_WORKERS,
        thread_name_prefix="cochl-sense-cloud-live-convert",
    )
    created_app.state.live_conversion_futures = set()
    created_app.state.live_conversion_lock = Lock()
    created_app.state.live_collection_manager = LiveCollectionManager(
        collected_root=_collected_root()
    )

    created_app.add_middleware(
        RequestBodyLimitMiddleware,
        settings_resolver=_settings_for_scope,
    )
    created_app.add_middleware(LocalAccessMiddleware)
    created_app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"],
    )
    created_app.add_middleware(SecurityHeadersMiddleware)

    created_app.add_api_route("/api/health", health, methods=["GET"])
    created_app.add_api_route(
        "/api/ready",
        ready,
        methods=["GET"],
        response_model=ReadinessResponse,
    )
    created_app.add_api_route(
        "/api/runtime-config",
        runtime_config,
        methods=["GET"],
        response_model=RuntimeConfigResponse,
    )
    created_app.add_api_route(
        "/api/analyze-recording",
        analyze_recording,
        methods=["POST"],
        response_model=AnalysisResponse,
    )
    created_app.add_api_route(
        "/api/analyze-live-chunk",
        analyze_live_chunk,
        methods=["POST"],
        response_model=LiveChunkAnalysisResponse,
    )
    created_app.add_api_route(
        "/api/live-session/end",
        end_live_session,
        methods=["POST"],
        response_model=LiveSessionEndResponse,
    )
    created_app.add_api_route(
        "/api/collected-sessions",
        get_collected_sessions,
        methods=["GET"],
        response_model=CollectedSessionsResponse,
    )
    created_app.add_api_route(
        "/api/collected-sessions/{session_id}/files/{filename}",
        get_collected_file,
        methods=["GET"],
    )
    created_app.add_api_route(
        "/api/collected-sessions/{session_id}",
        remove_collected_session,
        methods=["DELETE"],
        response_model=DeletionResponse,
    )
    created_app.add_api_route(
        "/api/collected-sessions/{session_id}/segments/{filename}",
        remove_collected_segment,
        methods=["DELETE"],
        response_model=DeletionResponse,
    )
    created_app.add_api_route(
        "/api/collected-sessions/{session_id}/gcs-upload",
        upload_collected_session_to_gcs,
        methods=["POST"],
        response_model=GcsSessionUploadResponse,
    )
    created_app.add_api_route(
        "/api/collected-sessions/{session_id}/gcs-upload/progress",
        upload_collected_session_to_gcs_with_progress,
        methods=["POST"],
    )

    if frontend_dist and (frontend_dist / "index.html").exists():
        assets_dir = frontend_dist / "assets"
        if assets_dir.exists():
            created_app.mount(
                "/assets",
                StaticFiles(directory=assets_dir),
                name="frontend-assets",
            )

        @created_app.get("/")
        def serve_index() -> FileResponse:
            return FileResponse(frontend_dist / "index.html")

        @created_app.get("/{path:path}")
        def serve_spa(path: str) -> FileResponse:
            if path == "api" or path.startswith("api/"):
                raise HTTPException(status_code=404, detail="API route not found.")
            requested = _safe_frontend_file(frontend_dist, path)
            if requested is not None:
                return FileResponse(requested)
            return FileResponse(frontend_dist / "index.html")

    return created_app


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def _settings_for_scope(scope: dict) -> Settings:
    return _settings_for_app(scope["app"])


def _settings_for_app(current_app: FastAPI) -> Settings:
    override = current_app.dependency_overrides.get(get_settings)
    return override() if override is not None else get_settings()


def _verify_recordings_storage(*, probe_write: bool = True) -> None:
    DEFAULT_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    if not os.access(DEFAULT_RECORDINGS_DIR, os.R_OK | os.W_OK | os.X_OK):
        raise PermissionError(DEFAULT_RECORDINGS_DIR)
    if not probe_write:
        return
    probe = DEFAULT_RECORDINGS_DIR / f".readiness-{secrets.token_hex(8)}.tmp"
    try:
        with probe.open("xb"):
            pass
    finally:
        probe.unlink(missing_ok=True)


def health() -> dict[str, str]:
    return {"status": "ok"}


def ready(settings: Settings = Depends(get_settings)) -> ReadinessResponse:
    failures: list[str] = []
    try:
        settings.validate_service_combination()
        settings.validate_timeouts()
        settings.validate_upload()
        settings.validate_collection()
        settings.validate_gcs()
    except ValueError as exc:
        failures.append(str(exc))
    if not settings.cochl_project_key:
        failures.append("COCHL_PROJECT_KEY is not configured.")
    try:
        # Lifespan startup already performed the write probe and acquired the
        # recordings lock. Readiness polling should not create and delete a new
        # protected-folder file on every request.
        _verify_recordings_storage(probe_write=False)
    except OSError as exc:
        failures.append(f"Recordings directory is not readable and writable: {exc}")
    if failures:
        raise HTTPException(status_code=503, detail=" ".join(failures))
    return ReadinessResponse(capabilities=_runtime_capabilities(settings))


def runtime_config(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> RuntimeConfigResponse:
    response.headers["Cache-Control"] = "no-store"
    return RuntimeConfigResponse(
        collection_confidence_threshold=settings.collection_confidence_threshold,
        api_token=request.app.state.api_token,
        capabilities=_runtime_capabilities(settings),
    )


def _runtime_capabilities(settings: Settings) -> RuntimeCapabilities:
    return RuntimeCapabilities(
        gcs=bool(
            settings.gcs_project_id
            and settings.gcs_bucket_name
            and settings.gcs_object_prefix
        ),
    )


def _safe_frontend_file(frontend_dist: Path, path: str) -> Path | None:
    frontend_root = frontend_dist.resolve()
    requested = (frontend_root / path).resolve()
    try:
        requested.relative_to(frontend_root)
    except ValueError:
        return None
    if requested.is_file():
        return requested
    return None


async def analyze_recording(
    request: Request,
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
) -> AnalysisResponse:
    if not settings.cochl_project_key:
        raise HTTPException(
            status_code=500,
            detail="Server is missing COCHL_PROJECT_KEY.",
        )

    started_at = perf_counter()
    try:
        source_path = await _save_upload(file, settings.max_upload_mb)
        provider = _provider(request.app, settings)
        prepared, raw_result = await _prepare_and_analyze_recording(
            request.app,
            provider,
            source_path,
            file.content_type,
        )
        processing_time_ms = int((perf_counter() - started_at) * 1000)
        return normalize_cochl_result(
            raw_result,
            duration_sec=None,
            content_type=prepared.content_type,
            services_used=settings.enabled_services(),
            processing_time_ms=processing_time_ms,
        )
    except EmptyUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except DuplicateUploadError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AudioConversionError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except ProviderBusyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ProviderTimeoutError, CochlProviderTimeoutError) as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except CochlContractError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Cochl recording analysis failed.")
        raise HTTPException(
            status_code=502,
            detail="Cochl analysis failed.",
        ) from exc


async def analyze_live_chunk(
    request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(...),
    sequence_id: int = Form(...),
    window_start_sec: float = Form(...),
    window_end_sec: float = Form(...),
    session_name: str = Form(""),
    settings: Settings = Depends(get_settings),
) -> LiveChunkAnalysisResponse:
    if not settings.cochl_project_key:
        raise HTTPException(
            status_code=500,
            detail="Server is missing COCHL_PROJECT_KEY.",
        )

    started_at = perf_counter()
    saved_path: Path | None = None
    live_file_handled = False
    try:
        _validate_live_chunk_metadata(sequence_id, window_start_sec, window_end_sec)
        saved_path = await _save_live_chunk_upload(
            file,
            settings.max_upload_mb,
            session_id,
            sequence_id,
            window_start_sec,
            window_end_sec,
        )
        provider = _provider(request.app, settings)
        raw_result = await _analyze_live_chunk_with_provider(
            request.app,
            provider,
            saved_path,
        )
        processing_time_ms = int((perf_counter() - started_at) * 1000)
        sound_events = normalize_sound_events(raw_result, offset_sec=window_start_sec)
        collection_status = None
        curation_progress = None
        if settings.collection_enabled:
            try:
                collection_status, curation_progress = await _collect_live_chunk(
                    request.app,
                    settings,
                    sequence_id=sequence_id,
                    window_start_sec=window_start_sec,
                    window_end_sec=window_end_sec,
                    saved_path=saved_path,
                    events=sound_events,
                    session_name=_clean_session_name(session_name),
                )
                live_file_handled = True
            except Exception:
                logger.exception(
                    "Collection failed for live chunk sequence %s.", sequence_id
                )
                await run_in_threadpool(_discard_live_chunk_file, saved_path)
                live_file_handled = True
        else:
            schedule_live_chunk_conversion(request.app, saved_path)
        return LiveChunkAnalysisResponse(
            sequence_id=sequence_id,
            window_start_sec=window_start_sec,
            window_end_sec=window_end_sec,
            sound_events=sound_events,
            processing_time_ms=processing_time_ms,
            collection_status=collection_status,
            curation_progress=curation_progress,
        )
    except EmptyUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except DuplicateUploadError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except ProviderBusyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ProviderTimeoutError, CochlProviderTimeoutError) as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except CochlContractError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Cochl live chunk analysis failed for sequence %s.", sequence_id)
        raise HTTPException(
            status_code=502,
            detail="Cochl live chunk analysis failed.",
        ) from exc
    finally:
        if settings.collection_enabled and saved_path is not None and not live_file_handled:
            await run_in_threadpool(_discard_live_chunk_file, saved_path)


async def end_live_session(
    request: Request,
    session_id: str = Form(...),
    session_name: str = Form(""),
) -> LiveSessionEndResponse:
    manager: LiveCollectionManager = request.app.state.live_collection_manager
    safe_session_id = _safe_live_session_id(session_id)
    try:
        return await run_in_threadpool(
            manager.end_session,
            safe_session_id,
            _clean_session_name(session_name),
        )
    except Exception as exc:
        logger.exception("Could not durably finalize live session %s.", safe_session_id)
        raise HTTPException(
            status_code=500,
            detail="Could not durably finalize the live session.",
        ) from exc


def _collected_root() -> Path:
    return DEFAULT_RECORDINGS_DIR / "collected"


def _clean_session_name(session_name: str) -> str | None:
    cleaned = session_name.strip()
    return cleaned[:100] or None


async def get_collected_sessions() -> CollectedSessionsResponse:
    sessions = await run_in_threadpool(list_collected_sessions, _collected_root())
    return CollectedSessionsResponse(sessions=sessions)


COLLECTED_FILE_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".json": "application/json",
}


async def get_collected_file(session_id: str, filename: str) -> FileResponse:
    session_dir = safe_collected_session_dir(_collected_root(), session_id)
    if (
        session_dir is None
        or Path(filename).name != filename
        or Path(filename).suffix.lower() not in COLLECTED_FILE_MEDIA_TYPES
    ):
        raise HTTPException(status_code=404, detail="Collected file not found.")

    requested_suffix = Path(filename).suffix.lower()
    stem = Path(filename).stem
    if filename == "session.json":
        requested = session_dir / filename
        if requested.is_file():
            return FileResponse(requested, media_type="application/json")
        raise HTTPException(status_code=404, detail="Collected file not found.")
    if not stem.startswith("segment-"):
        raise HTTPException(status_code=404, detail="Collected file not found.")

    metadata_path = session_dir / f"{stem}.json"
    audio_paths = [session_dir / f"{stem}.mp3", session_dir / f"{stem}.wav"]
    if requested_suffix == ".json":
        if metadata_path.is_file() and any(path.is_file() for path in audio_paths):
            return FileResponse(metadata_path, media_type="application/json")
        raise HTTPException(status_code=404, detail="Collected file not found.")
    if not metadata_path.is_file():
        # A conversion finishing after deletion must never make an orphaned
        # audio file visible again.
        raise HTTPException(status_code=404, detail="Collected file not found.")

    # Async MP3 conversion replaces a segment's WAV after it was listed, so a
    # stale audio URL falls back to the sibling extension with the same stem.
    candidates = [filename]
    if Path(filename).suffix.lower() != ".json":
        candidates += [
            f"{stem}{suffix}"
            for suffix in (".mp3", ".wav")
            if f"{stem}{suffix}" != filename
        ]
    for candidate in candidates:
        media_type = COLLECTED_FILE_MEDIA_TYPES.get(Path(candidate).suffix.lower())
        if media_type and (session_dir / candidate).is_file():
            return FileResponse(session_dir / candidate, media_type=media_type)
    raise HTTPException(status_code=404, detail="Collected file not found.")


async def remove_collected_session(session_id: str) -> DeletionResponse:
    _ensure_collected_session_closed(session_id)
    try:
        deleted = await run_in_threadpool(
            delete_collected_session,
            _collected_root(),
            session_id,
        )
    except OSError as exc:
        logger.exception("Could not delete collected session %s.", session_id)
        raise HTTPException(
            status_code=500,
            detail="Could not delete the collected session.",
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Collected session not found.")
    return DeletionResponse()


async def remove_collected_segment(session_id: str, filename: str) -> DeletionResponse:
    _ensure_collected_session_closed(session_id)
    try:
        deleted = await run_in_threadpool(
            delete_collected_segment,
            _collected_root(),
            session_id,
            filename,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.exception(
            "Could not delete collected segment %s from session %s.",
            filename,
            session_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Could not delete the collected segment safely.",
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Collected segment not found.")
    return DeletionResponse()


def _ensure_collected_session_closed(session_id: str) -> None:
    session_dir = safe_collected_session_dir(_collected_root(), session_id)
    if session_dir is None:
        raise HTTPException(status_code=404, detail="Collected session not found.")
    summary_path = session_dir / "session.json"
    if not summary_path.is_file():
        # Legacy collected sessions predate the live/closed state field.
        return
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=409,
            detail="세션 상태를 확인할 수 없어 삭제할 수 없습니다.",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=409,
            detail="세션 상태를 확인할 수 없어 삭제할 수 없습니다.",
        )
    ended_at = payload.get("ended_at")
    if ended_at is None:
        raise HTTPException(
            status_code=409,
            detail="녹음이 진행 중인 세션은 종료 후 삭제할 수 있습니다.",
        )
    if not isinstance(ended_at, str) or not ended_at.strip():
        raise HTTPException(
            status_code=409,
            detail="세션 상태를 확인할 수 없어 삭제할 수 없습니다.",
        )


async def upload_collected_session_to_gcs(
    request: Request,
    session_id: str,
    settings: Settings = Depends(get_settings),
) -> GcsSessionUploadResponse:
    session_dir = safe_collected_session_dir(_collected_root(), session_id)
    if session_dir is None:
        raise HTTPException(status_code=404, detail="Collected session not found.")

    try:
        settings.require_gcs_upload()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    project_id = settings.gcs_project_id or ""
    bucket_name = settings.gcs_bucket_name or ""
    try:
        uploader_id = settings.gcs_uploader_id or await run_in_threadpool(
            load_or_create_uploader_id,
            DEFAULT_RECORDINGS_DIR / ".gcs-uploader-id",
        )
        factory = request.app.state.gcs_storage_client_factory
        storage_client = (
            factory(settings)
            if factory is not None
            else await run_in_threadpool(create_storage_client, project_id)
        )
        result = await run_in_threadpool(
            upload_collected_session,
            collected_session_dir=session_dir,
            session_id=session_id,
            bucket_name=bucket_name,
            object_prefix=settings.gcs_object_prefix,
            uploader_id=uploader_id,
            storage_client=storage_client,
        )
        await run_in_threadpool(write_upload_marker, session_dir, result)
        return GcsSessionUploadResponse(
            session_id=session_id,
            object_prefix=result.object_prefix,
            snapshot_id=result.snapshot_id,
            uploaded_file_count=result.uploaded_file_count,
            existing_file_count=result.existing_file_count,
            total_size_bytes=result.total_size_bytes,
        )
    except GcsUploadConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GcsSessionStillOpenError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GcsUploadAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=422,
            detail="The collected session is incomplete.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GcsUploadFailedError as exc:
        logger.exception("GCS upload failed for session %s.", session_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except GcsUploadPersistenceError as exc:
        logger.exception("Could not persist GCS upload state for session %s.", session_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("GCS upload failed for session %s.", session_id)
        raise HTTPException(status_code=500, detail="GCS upload failed.") from exc


async def upload_collected_session_to_gcs_with_progress(
    request: Request,
    session_id: str,
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    session_dir = safe_collected_session_dir(_collected_root(), session_id)
    if session_dir is None:
        raise HTTPException(status_code=404, detail="Collected session not found.")

    try:
        settings.require_gcs_upload()
        uploader_id = settings.gcs_uploader_id or await run_in_threadpool(
            load_or_create_uploader_id,
            DEFAULT_RECORDINGS_DIR / ".gcs-uploader-id",
        )
        factory = request.app.state.gcs_storage_client_factory
        storage_client = (
            factory(settings)
            if factory is not None
            else await run_in_threadpool(
                create_storage_client,
                settings.gcs_project_id or "",
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GcsUploadConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GcsUploadAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Could not prepare GCS upload for session %s.", session_id)
        raise HTTPException(status_code=500, detail="GCS upload failed.") from exc

    return StreamingResponse(
        _gcs_upload_progress_stream(
            session_dir=session_dir,
            session_id=session_id,
            bucket_name=settings.gcs_bucket_name or "",
            object_prefix=settings.gcs_object_prefix,
            uploader_id=uploader_id,
            storage_client=storage_client,
        ),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


async def _gcs_upload_progress_stream(
    *,
    session_dir: Path,
    session_id: str,
    bucket_name: str,
    object_prefix: str,
    uploader_id: str,
    storage_client: StorageClient,
) -> AsyncIterator[str]:
    event_queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
    event_loop = asyncio.get_running_loop()

    def report_progress(progress: GcsUploadFileProgress) -> None:
        event = {
            "type": "progress",
            "object_name": progress.object_name,
            "source_filename": progress.source_filename,
            "file_status": progress.file_status,
            "completed_file_count": progress.completed_file_count,
            "total_file_count": progress.total_file_count,
        }
        asyncio.run_coroutine_threadsafe(event_queue.put(event), event_loop).result()

    async def run_upload() -> None:
        try:
            result = await run_in_threadpool(
                upload_collected_session,
                collected_session_dir=session_dir,
                session_id=session_id,
                bucket_name=bucket_name,
                object_prefix=object_prefix,
                uploader_id=uploader_id,
                storage_client=storage_client,
                progress_callback=report_progress,
            )
            await run_in_threadpool(write_upload_marker, session_dir, result)
            await event_queue.put(
                {
                    "type": "complete",
                    "status": "uploaded",
                    "session_id": session_id,
                    "object_prefix": result.object_prefix,
                    "snapshot_id": result.snapshot_id,
                    "uploaded_file_count": result.uploaded_file_count,
                    "existing_file_count": result.existing_file_count,
                    "total_size_bytes": result.total_size_bytes,
                }
            )
        except Exception as exc:
            await event_queue.put(
                {
                    "type": "error",
                    "message": _gcs_upload_stream_error(exc, session_id),
                }
            )
        finally:
            await event_queue.put(None)

    upload_task = asyncio.create_task(run_upload())
    _gcs_progress_upload_tasks.add(upload_task)
    upload_task.add_done_callback(_gcs_progress_upload_tasks.discard)
    try:
        while True:
            event = await event_queue.get()
            if event is None:
                break
            yield f"{json.dumps(event, ensure_ascii=False)}\n"
    finally:
        if upload_task.done():
            await upload_task


def _gcs_upload_stream_error(exc: Exception, session_id: str) -> str:
    if isinstance(
        exc,
        (
            GcsUploadConfigurationError,
            GcsSessionStillOpenError,
            GcsUploadAuthorizationError,
            GcsUploadFailedError,
            GcsUploadPersistenceError,
            ValueError,
        ),
    ):
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return "The collected session is incomplete."
    logger.exception(
        "GCS upload progress stream failed for session %s.",
        session_id,
        exc_info=exc,
    )
    return "GCS upload failed."


async def _collect_live_chunk(
    current_app: FastAPI,
    settings: Settings,
    *,
    sequence_id: int,
    window_start_sec: float,
    window_end_sec: float,
    saved_path: Path,
    events: list[SoundEvent],
    session_name: str | None = None,
) -> tuple[str, LiveCurationProgress | None]:
    manager: LiveCollectionManager = current_app.state.live_collection_manager
    safe_session_id = saved_path.parent.name
    output_dir = _collected_root() / safe_session_id

    def schedule_segment_conversion(wav_path: Path) -> None:
        schedule_live_chunk_conversion(current_app, wav_path)

    def add_chunk() -> tuple[str, LiveCurationProgress | None]:
        status = manager.add_chunk(
            safe_session_id,
            output_dir=output_dir,
            policy=policy_from_settings(settings),
            mp3_scheduler=schedule_segment_conversion,
            session_name=session_name,
            sequence_id=sequence_id,
            window_start_sec=window_start_sec,
            window_end_sec=window_end_sec,
            wav_path=saved_path,
            events=events,
        )
        return status, manager.get_curation_progress(safe_session_id)

    return await run_in_threadpool(add_chunk)


async def _save_upload(
    file: UploadFile,
    max_upload_mb: int,
    recordings_dir: Path | None = None,
) -> Path:
    suffix = _upload_suffix(file.content_type, file.filename)
    target_dir = recordings_dir or DEFAULT_RECORDINGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    while True:
        target_path = _available_recording_path(target_dir, file.filename, suffix)
        try:
            await run_in_threadpool(
                _write_upload_stream,
                file.file,
                target_path,
                max_upload_mb,
                "Recording file is empty.",
            )
        except FileExistsError:
            continue
        return target_path


async def _save_live_chunk_upload(
    file: UploadFile,
    max_upload_mb: int,
    session_id: str,
    sequence_id: int,
    window_start_sec: float,
    window_end_sec: float,
    recordings_dir: Path | None = None,
) -> Path:
    safe_session_id = _safe_live_session_id(session_id)
    live_dir = (recordings_dir or DEFAULT_RECORDINGS_DIR) / "live" / safe_session_id
    live_dir.mkdir(parents=True, exist_ok=True)
    filename = f"chunk-{sequence_id:06d}-{window_start_sec:.3f}-{window_end_sec:.3f}.wav"
    target_path = live_dir / filename
    try:
        await run_in_threadpool(
            _write_upload_stream,
            file.file,
            target_path,
            max_upload_mb,
            "Live chunk file is empty.",
        )
    except FileExistsError as exc:
        raise DuplicateUploadError(
            f"Live chunk {sequence_id} already exists for this session."
        ) from exc
    return target_path


def _safe_live_session_id(session_id: str) -> str:
    if not LIVE_SESSION_ID_PATTERN.fullmatch(session_id) or session_id in {".", ".."}:
        raise HTTPException(
            status_code=422,
            detail=(
                "Session id must start with an ASCII letter or number and use "
                "only letters, numbers, dots, underscores, or hyphens (128 max)."
            ),
        )
    return session_id


def _validate_live_chunk_metadata(
    sequence_id: int,
    window_start_sec: float,
    window_end_sec: float,
) -> None:
    if sequence_id < 1:
        raise HTTPException(status_code=422, detail="Sequence id must be positive.")
    if not isfinite(window_start_sec) or not isfinite(window_end_sec):
        raise HTTPException(status_code=422, detail="Live window times must be finite.")
    if window_start_sec < 0 or window_end_sec <= window_start_sec:
        raise HTTPException(
            status_code=422,
            detail="Live window end must be greater than its non-negative start.",
        )


def _write_upload_stream(
    source: BinaryIO,
    target_path: Path,
    max_upload_mb: int,
    empty_message: str,
) -> None:
    total_bytes = 0
    try:
        with target_path.open("xb") as target:
            while chunk := source.read(UPLOAD_READ_CHUNK_BYTES):
                total_bytes += len(chunk)
                validate_upload_size(total_bytes, max_upload_mb)
                target.write(chunk)
        if total_bytes == 0:
            raise EmptyUploadError(empty_message)
    except FileExistsError:
        raise
    except Exception:
        target_path.unlink(missing_ok=True)
        raise


def _discard_live_chunk_file(wav_path: Path) -> None:
    try:
        wav_path.unlink(missing_ok=True)
        wav_path.parent.rmdir()
    except OSError:
        # The directory may still contain other in-flight chunks.
        pass


def convert_live_chunk_to_mp3(wav_path: Path) -> None:
    mp3_path = wav_path.with_suffix(".mp3")
    temp_mp3_path = mp3_path.with_name(f".{mp3_path.stem}.tmp.mp3")
    try:
        convert_to_mp3(wav_path, temp_mp3_path)
        if wav_path.stem.startswith("segment-"):
            published = publish_segment_conversion(wav_path, temp_mp3_path)
        else:
            temp_mp3_path.replace(mp3_path)
            published = True
        if not published:
            return
    except AudioConversionError:
        return
    finally:
        temp_mp3_path.unlink(missing_ok=True)
    if mp3_path.exists():
        wav_path.unlink(missing_ok=True)


def schedule_live_chunk_conversion(current_app: FastAPI, wav_path: Path) -> bool:
    futures: set[Future] = current_app.state.live_conversion_futures
    lock: Lock = current_app.state.live_conversion_lock
    executor: ThreadPoolExecutor = current_app.state.live_conversion_executor

    with lock:
        if len(futures) >= LIVE_CONVERSION_MAX_PENDING:
            logger.warning(
                "Skipping live chunk conversion for %s because %d conversions are pending.",
                wav_path,
                len(futures),
            )
            return False
        try:
            future = executor.submit(convert_live_chunk_to_mp3, wav_path)
        except RuntimeError:
            logger.exception("Could not schedule live chunk conversion for %s.", wav_path)
            return False
        futures.add(future)

    _track_live_conversion_future(current_app, future, wav_path)
    return True


def _track_live_conversion_future(
    current_app: FastAPI,
    future: Future,
    wav_path: Path,
) -> None:
    futures: set[Future] = current_app.state.live_conversion_futures
    lock: Lock = current_app.state.live_conversion_lock

    def finish(done_future: Future) -> None:
        try:
            done_future.result()
        except Exception:
            logger.exception("Live chunk conversion failed for %s.", wav_path)
        finally:
            with lock:
                futures.discard(done_future)

    future.add_done_callback(finish)


def _upload_suffix(content_type: str | None, filename: str | None) -> str:
    suffix = extension_for_content_type(content_type)
    if suffix != ".bin":
        return suffix
    return Path(filename or "").suffix or suffix


def _available_recording_path(recordings_dir: Path, filename: str | None, suffix: str) -> Path:
    safe_name = Path(filename or "").name
    if not safe_name:
        safe_name = f"recording{suffix}"

    candidate = recordings_dir / safe_name
    if candidate.suffix.lower() != suffix.lower():
        candidate = candidate.with_suffix(suffix)
    if not candidate.exists():
        return candidate

    for index in count(2):
        next_candidate = candidate.with_name(f"{candidate.stem}-{index}{candidate.suffix}")
        if not next_candidate.exists():
            return next_candidate


def _provider(current_app: FastAPI, settings: Settings):
    factory = getattr(current_app.state, "provider_factory", None)
    if factory is not None:
        return factory(settings)
    return CochlProvider(settings)


async def _analyze_live_chunk_with_provider(
    current_app: FastAPI,
    provider: CochlProvider,
    saved_path: Path,
):
    limiter = getattr(current_app.state, "live_provider_limiter", None)
    settings = provider.settings
    timeout_sec = getattr(
        settings,
        "cochl_live_timeout_sec",
        DEFAULT_LIVE_PROVIDER_TIMEOUT_SEC,
    )

    def analyze():
        return provider.analyze_live_chunk(saved_path)

    async def run():
        return await _run_bounded_provider_job(
            current_app,
            kind="live",
            job=analyze,
            timeout_sec=timeout_sec,
        )

    return await _run_provider_job_with_deadline(
        kind="live",
        run=run,
        limiter=limiter,
        timeout_sec=timeout_sec,
    )


async def _prepare_and_analyze_recording(
    current_app: FastAPI,
    provider: CochlProvider,
    source_path: Path,
    content_type: str | None,
):
    def prepare_and_analyze():
        prepared = prepare_audio_for_cochl(
            source_path,
            content_type,
            source_path.name,
        )
        try:
            return prepared, provider.analyze_file(prepared.path)
        finally:
            if prepared.path != source_path:
                try:
                    prepared.path.unlink(missing_ok=True)
                except OSError:
                    logger.exception(
                        "Could not remove provider conversion temp %s.", prepared.path
                    )

    limiter = getattr(current_app.state, "recording_provider_limiter", None)
    settings = provider.settings
    timeout_sec = getattr(
        settings,
        "cochl_recording_timeout_sec",
        DEFAULT_RECORDING_PROVIDER_TIMEOUT_SEC,
    )

    async def run():
        return await _run_bounded_provider_job(
            current_app,
            kind="recording",
            job=prepare_and_analyze,
            timeout_sec=timeout_sec,
        )

    return await _run_provider_job_with_deadline(
        kind="recording",
        run=run,
        limiter=limiter,
        timeout_sec=timeout_sec,
    )


async def _run_provider_job_with_deadline(
    *,
    kind: str,
    run,
    limiter: CapacityLimiter | None,
    timeout_sec: float,
):
    """Apply one request deadline across capacity wait and provider work."""

    async def limited_run():
        if limiter is None:
            return await run()
        async with limiter:
            return await run()

    try:
        return await asyncio.wait_for(limited_run(), timeout=timeout_sec)
    except ProviderTimeoutError:
        raise
    except TimeoutError as exc:
        raise ProviderTimeoutError(
            f"Cochl {kind} analysis exceeded the {timeout_sec:g} second deadline."
        ) from exc


async def _run_bounded_provider_job(
    current_app: FastAPI,
    *,
    kind: str,
    job,
    timeout_sec: float,
):
    executor: ThreadPoolExecutor = getattr(
        current_app.state, f"{kind}_provider_executor"
    )
    futures: set[Future] = getattr(current_app.state, f"{kind}_provider_futures")
    lock: Lock = getattr(current_app.state, f"{kind}_provider_lock")
    max_pending = (
        LIVE_PROVIDER_MAX_CONCURRENCY
        if kind == "live"
        else RECORDING_PROVIDER_MAX_CONCURRENCY
    )
    event_loop = asyncio.get_running_loop()
    async_result = event_loop.create_future()

    with lock:
        if len(futures) >= max_pending:
            raise ProviderBusyError(
                f"Cochl {kind} analysis capacity is temporarily exhausted."
            )
        try:
            provider_future = executor.submit(job)
        except RuntimeError as exc:
            raise ProviderBusyError("Cochl analysis service is shutting down.") from exc
        futures.add(provider_future)

    def finish(done_future: Future) -> None:
        with lock:
            futures.discard(done_future)
        try:
            result = done_future.result()
        except BaseException as exc:
            callback = async_result.set_exception
            value = exc
        else:
            callback = async_result.set_result
            value = result

        def complete() -> None:
            if not async_result.done():
                callback(value)

        try:
            event_loop.call_soon_threadsafe(complete)
        except RuntimeError:
            # The serving event loop may already have closed during shutdown.
            pass

    provider_future.add_done_callback(finish)
    try:
        return await asyncio.wait_for(asyncio.shield(async_result), timeout=timeout_sec)
    except TimeoutError as exc:
        async_result.cancel()
        raise ProviderTimeoutError(
            f"Cochl {kind} analysis exceeded the {timeout_sec:g} second deadline."
        ) from exc
    except asyncio.CancelledError:
        async_result.cancel()
        raise


app = create_app()
