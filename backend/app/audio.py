from __future__ import annotations

import os
import shutil
# ffmpeg is invoked below without a shell from a resolved executable path.
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class UploadTooLargeError(ValueError):
    pass


class EmptyUploadError(ValueError):
    pass


class DuplicateUploadError(ValueError):
    pass


class AudioConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedAudio:
    path: Path
    content_type: str


COCHL_SUPPORTED_CONTENT_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/flac",
    "audio/ogg",
    "application/ogg",
}

COCHL_SUPPORTED_SUFFIXES = {".mp3", ".wav", ".flac", ".ogg"}

CONTENT_TYPE_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/webm;codecs=opus": ".webm",
    "audio/mp4": ".m4a",
}

FFMPEG_TIMEOUT_SEC = 120
LIVE_FFMPEG_TIMEOUT_SEC = 5
LIVE_PROVIDER_VORBIS_QUALITY = 3
FFMPEG_FALLBACK_PATHS = (
    Path("/opt/homebrew/bin/ffmpeg"),
    Path("/usr/local/bin/ffmpeg"),
    Path("/opt/local/bin/ffmpeg"),
)


def find_ffmpeg_executable() -> str | None:
    """Find ffmpeg even when a macOS app starts with LaunchServices' small PATH."""

    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered
    for candidate in FFMPEG_FALLBACK_PATHS:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run_ffmpeg(
    arguments: list[str],
    *,
    timeout_sec: int,
    unavailable_message: str,
    failure_message: str,
) -> None:
    ffmpeg = find_ffmpeg_executable()
    if not ffmpeg:
        raise AudioConversionError(unavailable_message)

    try:
        # No shell is used, and the executable is resolved from trusted paths.
        subprocess.run(  # nosec B603
            [ffmpeg, "-nostdin", "-y", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise AudioConversionError(failure_message) from exc


def normalized_content_type(content_type: str | None) -> str:
    if not content_type:
        return "application/octet-stream"
    return content_type.split(";")[0].strip().lower()


def extension_for_content_type(content_type: str | None) -> str:
    normalized = normalized_content_type(content_type)
    return CONTENT_TYPE_EXTENSIONS.get(normalized, ".bin")


def is_cochl_supported_audio(content_type: str | None, filename: str | None) -> bool:
    normalized = normalized_content_type(content_type)
    suffix = Path(filename or "").suffix.lower()
    return normalized in COCHL_SUPPORTED_CONTENT_TYPES or suffix in COCHL_SUPPORTED_SUFFIXES


def validate_upload_size(size_bytes: int, max_upload_mb: int) -> None:
    max_bytes = max_upload_mb * 1024 * 1024
    if size_bytes > max_bytes:
        raise UploadTooLargeError(f"Upload exceeds {max_upload_mb} MB limit.")


def prepare_audio_for_cochl(
    source_path: Path,
    content_type: str | None,
    filename: str | None,
) -> PreparedAudio:
    normalized = normalized_content_type(content_type)
    if is_cochl_supported_audio(normalized, filename):
        return PreparedAudio(
            path=source_path,
            content_type=normalized,
        )

    # Never derive the output as ``source.with_suffix('.wav')``: a WebM named
    # clip.webm may be uploaded beside a user's existing clip.wav. Conversion
    # is provider scratch data, so give it an unguessable hidden name and let
    # the request lifecycle remove it after analysis.
    converted_path = source_path.with_name(
        f".{source_path.name}.{uuid4().hex}.cochl.wav"
    )
    try:
        convert_to_wav(source_path, converted_path)
    except Exception:
        converted_path.unlink(missing_ok=True)
        raise
    return PreparedAudio(
        path=converted_path,
        content_type="audio/wav",
    )


def prepare_live_audio_for_cochl(source_path: Path) -> PreparedAudio:
    """Build a small provider-only copy while preserving the captured WAV."""

    converted_path = source_path.with_name(
        f".{source_path.name}.{uuid4().hex}.cochl.ogg"
    )
    try:
        convert_live_to_ogg(source_path, converted_path)
    except AudioConversionError:
        converted_path.unlink(missing_ok=True)
        return PreparedAudio(path=source_path, content_type="audio/wav")
    return PreparedAudio(path=converted_path, content_type="audio/ogg")


def convert_to_wav(input_path: Path, output_path: Path) -> None:
    _run_ffmpeg(
        [
            "-i",
            str(input_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(output_path),
        ],
        timeout_sec=FFMPEG_TIMEOUT_SEC,
        unavailable_message=(
            "ffmpeg is required to convert this browser recording format to WAV."
        ),
        failure_message="Failed to convert recording to WAV.",
    )


def convert_live_to_ogg(input_path: Path, output_path: Path) -> None:
    _run_ffmpeg(
        [
            "-i",
            str(input_path),
            "-ac",
            "1",
            "-c:a",
            "libvorbis",
            "-q:a",
            str(LIVE_PROVIDER_VORBIS_QUALITY),
            str(output_path),
        ],
        timeout_sec=LIVE_FFMPEG_TIMEOUT_SEC,
        unavailable_message=(
            "ffmpeg is unavailable; live analysis will use the original WAV."
        ),
        failure_message="Failed to create the optimized live-analysis transport.",
    )

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise AudioConversionError(
            "Live-analysis transport conversion produced no audio."
        )


def convert_to_mp3(input_path: Path, output_path: Path) -> None:
    _run_ffmpeg(
        [
            "-i",
            str(input_path),
            "-ar",
            "44100",
            "-ac",
            "1",
            "-b:a",
            "128k",
            str(output_path),
        ],
        timeout_sec=FFMPEG_TIMEOUT_SEC,
        unavailable_message="ffmpeg is required to convert recording to MP3.",
        failure_message="Failed to convert recording to MP3.",
    )
