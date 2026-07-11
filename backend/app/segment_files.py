from __future__ import annotations

import re
from pathlib import Path

_SEGMENT_INDEX_PATTERN = re.compile(r"^segment-(\d+)(?:-|$)")


def make_segment_stem(index: int, start_sec: float, end_sec: float) -> str:
    if index <= 0:
        raise ValueError("Segment index must be positive.")
    return f"segment-{index:03d}-{start_sec:.3f}-{end_sec:.3f}"


def sorted_segment_metadata_paths(session_dir: Path) -> list[Path]:
    def sort_key(path: Path) -> tuple[int, int | str, str]:
        match = _SEGMENT_INDEX_PATTERN.match(path.stem)
        if match:
            return (0, int(match.group(1)), path.name)
        return (1, path.name, path.name)

    return sorted(session_dir.glob("segment-*.json"), key=sort_key)


def resolve_segment_audio(session_dir: Path, stem: str) -> Path | None:
    for suffix in (".mp3", ".wav"):
        candidate = session_dir / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None
