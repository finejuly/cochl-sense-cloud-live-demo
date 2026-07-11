from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


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

    converted_path = source_path.with_suffix(".wav")
    convert_to_wav(source_path, converted_path)
    return PreparedAudio(
        path=converted_path,
        content_type="audio/wav",
    )


def convert_to_wav(input_path: Path, output_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioConversionError(
            "ffmpeg is required to convert this browser recording format to WAV."
        )

    try:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(input_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise AudioConversionError("Failed to convert recording to WAV.") from exc


def convert_to_mp3(input_path: Path, output_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioConversionError("ffmpeg is required to convert recording to MP3.")

    try:
        subprocess.run(
            [
                ffmpeg,
                "-y",
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
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise AudioConversionError("Failed to convert recording to MP3.") from exc
