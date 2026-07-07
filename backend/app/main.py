from __future__ import annotations

import logging
import shutil
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import lru_cache
from itertools import count
from pathlib import Path
from threading import Lock
from time import perf_counter

from anyio import CapacityLimiter
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from backend.app.audio import (
    AudioConversionError,
    UploadTooLargeError,
    convert_to_mp3,
    extension_for_content_type,
    prepare_audio_for_cochl,
    validate_upload_size,
)
from backend.app.cochl_provider import CochlProvider
from backend.app.collection import (
    LiveCollectionManager,
    delete_collected_segment,
    delete_collected_session,
    list_collected_sessions,
    policy_from_settings,
    safe_collected_session_dir,
)
from backend.app.config import Settings
from backend.app.models import (
    AnalysisResponse,
    CollectedSessionsResponse,
    DeletionResponse,
    LiveChunkAnalysisResponse,
    LiveSessionEndResponse,
    SoundEvent,
)
from backend.app.normalization import normalize_cochl_result, normalize_sound_events

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
DEFAULT_RECORDINGS_DIR = PROJECT_ROOT / "recordings"
LIVE_PROVIDER_MAX_CONCURRENCY = 10
LIVE_CONVERSION_MAX_WORKERS = 2
LIVE_CONVERSION_MAX_PENDING = 32
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(current_app: FastAPI):
    cleanup_orphan_live_chunks()
    try:
        yield
    finally:
        current_app.state.live_conversion_executor.shutdown(
            wait=False,
            cancel_futures=True,
        )


def cleanup_orphan_live_chunks() -> None:
    """Removes live chunk staging left behind by a previous process.

    With collection enabled, `recordings/live/` only holds chunks awaiting
    classification; anything there at startup is an orphan from a crashed or
    restarted server. With collection disabled, live chunks are intentional
    debug output and must be kept.
    """
    try:
        settings = get_settings()
    except Exception:
        return
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


def create_app(frontend_dist: Path | None = DEFAULT_FRONTEND_DIST) -> FastAPI:
    created_app = FastAPI(
        title="Cochl.Sense Cloud Live Demo API",
        version="0.1.0",
        lifespan=lifespan,
    )
    created_app.state.provider_factory = None
    created_app.state.live_provider_limiter = CapacityLimiter(LIVE_PROVIDER_MAX_CONCURRENCY)
    created_app.state.live_conversion_executor = ThreadPoolExecutor(
        max_workers=LIVE_CONVERSION_MAX_WORKERS,
        thread_name_prefix="cochl-sense-cloud-live-convert",
    )
    created_app.state.live_conversion_futures = set()
    created_app.state.live_conversion_lock = Lock()
    created_app.state.live_collection_manager = LiveCollectionManager()

    created_app.add_api_route("/api/health", health, methods=["GET"])
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
            requested = _safe_frontend_file(frontend_dist, path)
            if requested is not None:
                return FileResponse(requested)
            return FileResponse(frontend_dist / "index.html")

    return created_app


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def health() -> dict[str, str]:
    return {"status": "ok"}


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
        prepared = prepare_audio_for_cochl(
            source_path,
            file.content_type,
            source_path.name,
        )

        provider = _provider(request.app, settings)
        raw_result = provider.analyze_file(prepared.path)
        processing_time_ms = int((perf_counter() - started_at) * 1000)
        return normalize_cochl_result(
            raw_result,
            duration_sec=None,
            content_type=prepared.content_type,
            services_used=settings.enabled_services(),
            processing_time_ms=processing_time_ms,
        )
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except AudioConversionError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
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
    try:
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
        if settings.collection_enabled:
            try:
                collection_status = await _collect_live_chunk(
                    request.app,
                    settings,
                    sequence_id=sequence_id,
                    window_start_sec=window_start_sec,
                    window_end_sec=window_end_sec,
                    saved_path=saved_path,
                    events=sound_events,
                    session_name=_clean_session_name(session_name),
                )
            except Exception:
                logger.exception(
                    "Collection failed for live chunk sequence %s.", sequence_id
                )
        else:
            schedule_live_chunk_conversion(request.app, saved_path)
        return LiveChunkAnalysisResponse(
            sequence_id=sequence_id,
            window_start_sec=window_start_sec,
            window_end_sec=window_end_sec,
            sound_events=sound_events,
            processing_time_ms=processing_time_ms,
            collection_status=collection_status,
        )
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Cochl live chunk analysis failed for sequence %s.", sequence_id)
        raise HTTPException(
            status_code=502,
            detail="Cochl live chunk analysis failed.",
        ) from exc


async def end_live_session(
    request: Request,
    session_id: str = Form(...),
    session_name: str = Form(""),
) -> LiveSessionEndResponse:
    manager: LiveCollectionManager = request.app.state.live_collection_manager
    safe_session_id = _safe_live_session_id(session_id)
    return await run_in_threadpool(
        manager.end_session,
        safe_session_id,
        _clean_session_name(session_name),
    )


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

    # Async MP3 conversion replaces a segment's WAV after it was listed, so a
    # stale audio URL falls back to the sibling extension with the same stem.
    stem = Path(filename).stem
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
    deleted = await run_in_threadpool(
        delete_collected_session,
        _collected_root(),
        session_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Collected session not found.")
    return DeletionResponse()


async def remove_collected_segment(session_id: str, filename: str) -> DeletionResponse:
    deleted = await run_in_threadpool(
        delete_collected_segment,
        _collected_root(),
        session_id,
        filename,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Collected segment not found.")
    return DeletionResponse()


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
) -> str:
    manager: LiveCollectionManager = current_app.state.live_collection_manager
    safe_session_id = saved_path.parent.name
    output_dir = _collected_root() / safe_session_id

    def schedule_segment_conversion(wav_path: Path) -> None:
        schedule_live_chunk_conversion(current_app, wav_path)

    def add_chunk() -> str:
        return manager.add_chunk(
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

    return await run_in_threadpool(add_chunk)


async def _save_upload(
    file: UploadFile,
    max_upload_mb: int,
    recordings_dir: Path | None = None,
) -> Path:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Recording file is empty.")
    validate_upload_size(len(data), max_upload_mb)

    suffix = _upload_suffix(file.content_type, file.filename)
    target_dir = recordings_dir or DEFAULT_RECORDINGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _available_recording_path(target_dir, file.filename, suffix)
    target_path.write_bytes(data)
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
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Live chunk file is empty.")
    validate_upload_size(len(data), max_upload_mb)

    safe_session_id = _safe_live_session_id(session_id)
    live_dir = (recordings_dir or DEFAULT_RECORDINGS_DIR) / "live" / safe_session_id
    live_dir.mkdir(parents=True, exist_ok=True)
    filename = f"chunk-{sequence_id:06d}-{window_start_sec:.3f}-{window_end_sec:.3f}.wav"
    target_path = live_dir / filename
    target_path.write_bytes(data)
    return target_path


def _safe_live_session_id(session_id: str) -> str:
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in {"_", "-", "."}) else "_"
        for char in session_id
    ).lstrip(".")
    return safe or "session"


def convert_live_chunk_to_mp3(wav_path: Path) -> None:
    mp3_path = wav_path.with_suffix(".mp3")
    try:
        convert_to_mp3(wav_path, mp3_path)
    except AudioConversionError:
        return
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
    if limiter is None:
        return await run_in_threadpool(provider.analyze_live_chunk, saved_path)
    async with limiter:
        return await run_in_threadpool(provider.analyze_live_chunk, saved_path)


app = create_app()
