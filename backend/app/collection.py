from __future__ import annotations

import json
import logging
import wave
from bisect import insort
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic

from backend.app.config import Settings
from backend.app.models import (
    CollectedSegmentSummary,
    LiveSessionEndResponse,
    SoundEvent,
)

logger = logging.getLogger(__name__)

CHUNK_COLLECTED = "collected"
CHUNK_DISCARDED_SILENT = "discarded_silent"
CHUNK_DISCARDED_SPEECH = "discarded_speech"

DEFAULT_REORDER_HOLD_BACK_SEC = 6.0
DEFAULT_STALE_SESSION_SEC = 600.0
_TIME_EPSILON_SEC = 1e-3


@dataclass(frozen=True)
class CollectionPolicy:
    confidence_threshold: float = 0.5
    exclude_label_keywords: tuple[str, ...] = ()
    max_segment_sec: float = 20.0
    reorder_hold_back_sec: float = DEFAULT_REORDER_HOLD_BACK_SEC


def policy_from_settings(settings: Settings) -> CollectionPolicy:
    return CollectionPolicy(
        confidence_threshold=settings.collection_confidence_threshold,
        exclude_label_keywords=settings.collection_exclude_label_keywords,
        max_segment_sec=settings.collection_max_segment_sec,
    )


def is_privacy_sensitive_label(label: str, exclude_keywords: Sequence[str]) -> bool:
    lowered = label.lower()
    return any(keyword in lowered for keyword in exclude_keywords)


def classify_chunk_events(
    events: Sequence[SoundEvent],
    policy: CollectionPolicy,
) -> str:
    """Decide whether a live chunk is worth collecting.

    Privacy wins over everything: any speech-like label discards the chunk,
    regardless of confidence. Otherwise the chunk is kept when at least one
    event clears the confidence threshold (events without a confidence are
    trusted as detected by Cochl).
    """
    has_meaningful_event = False
    for event in events:
        if is_privacy_sensitive_label(event.label, policy.exclude_label_keywords):
            return CHUNK_DISCARDED_SPEECH
        if event.confidence is None or event.confidence >= policy.confidence_threshold:
            has_meaningful_event = True
    return CHUNK_COLLECTED if has_meaningful_event else CHUNK_DISCARDED_SILENT


@dataclass(order=True)
class _ChunkEntry:
    window_start_sec: float
    sequence_id: int
    window_end_sec: float = field(compare=False)
    wav_path: Path = field(compare=False)
    events: list[SoundEvent] = field(compare=False, default_factory=list)
    decision: str = field(compare=False, default=CHUNK_COLLECTED)


class SegmentCollector:
    """Assembles kept live chunks of one session into <= max_segment_sec files.

    Chunk analyses can complete out of order, so entries first land in a
    small reorder buffer sorted by window start and are only folded into
    segments once the watermark (max window end seen minus a hold-back)
    passes them. `end_session` flushes everything.
    """

    def __init__(
        self,
        session_id: str,
        output_dir: Path,
        policy: CollectionPolicy,
        mp3_scheduler: Callable[[Path], object] | None = None,
    ):
        self.session_id = session_id
        self.output_dir = output_dir
        self.policy = policy
        self.mp3_scheduler = mp3_scheduler
        self.last_activity_monotonic = monotonic()
        self._lock = Lock()
        self._pending: list[_ChunkEntry] = []
        self._max_window_end_seen = 0.0
        self._segment_chunks: list[_ChunkEntry] = []
        self._segment_index = 0
        self._segments: list[CollectedSegmentSummary] = []
        self._kept_chunk_count = 0
        self._discarded_silent_count = 0
        self._discarded_speech_count = 0
        self._source_dirs: set[Path] = set()
        self._ended = False

    def add_chunk(
        self,
        *,
        sequence_id: int,
        window_start_sec: float,
        window_end_sec: float,
        wav_path: Path,
        events: Sequence[SoundEvent],
    ) -> str:
        decision = classify_chunk_events(events, self.policy)
        entry = _ChunkEntry(
            window_start_sec=window_start_sec,
            sequence_id=sequence_id,
            window_end_sec=window_end_sec,
            wav_path=wav_path,
            events=list(events),
            decision=decision,
        )

        with self._lock:
            if self._ended:
                # Late chunk after the session was finalized: apply the
                # decision standalone so the file never lingers unbounded.
                self._delete_chunk_file(entry)
                return decision
            self.last_activity_monotonic = monotonic()
            self._source_dirs.add(wav_path.parent)
            insort(self._pending, entry)
            self._max_window_end_seen = max(self._max_window_end_seen, window_end_sec)
            watermark = self._max_window_end_seen - self.policy.reorder_hold_back_sec
            while self._pending and self._pending[0].window_start_sec <= watermark:
                self._process_entry(self._pending.pop(0))
        return decision

    def end_session(self) -> LiveSessionEndResponse:
        with self._lock:
            self._ended = True
            while self._pending:
                self._process_entry(self._pending.pop(0))
            self._finalize_current_segment("session_end")
            summary = LiveSessionEndResponse(
                session_id=self.session_id,
                segment_count=len(self._segments),
                total_collected_duration_sec=round(
                    sum(segment.duration_sec for segment in self._segments), 3
                ),
                kept_chunk_count=self._kept_chunk_count,
                discarded_silent_chunk_count=self._discarded_silent_count,
                discarded_speech_chunk_count=self._discarded_speech_count,
                segments=list(self._segments),
            )
            self._write_session_summary(summary)
            self._cleanup_source_dirs()
        return summary

    def _process_entry(self, entry: _ChunkEntry) -> None:
        if entry.decision == CHUNK_DISCARDED_SILENT:
            self._discarded_silent_count += 1
            self._delete_chunk_file(entry)
            return
        if entry.decision == CHUNK_DISCARDED_SPEECH:
            self._discarded_speech_count += 1
            self._delete_chunk_file(entry)
            # Never let one collected file span across a speech region.
            self._finalize_current_segment("speech_boundary")
            return

        self._kept_chunk_count += 1
        if self._segment_chunks:
            current_end = max(chunk.window_end_sec for chunk in self._segment_chunks)
            segment_start = min(chunk.window_start_sec for chunk in self._segment_chunks)
            if entry.window_start_sec > current_end + _TIME_EPSILON_SEC:
                self._finalize_current_segment("gap")
            elif (
                entry.window_end_sec - segment_start
                > self.policy.max_segment_sec + _TIME_EPSILON_SEC
            ):
                self._finalize_current_segment("max_duration")
        self._segment_chunks.append(entry)

    def _finalize_current_segment(self, reason: str) -> None:
        chunks = self._segment_chunks
        self._segment_chunks = []
        if not chunks:
            return

        try:
            merged = _merge_chunk_audio(chunks)
        except Exception:
            logger.exception(
                "Failed to merge %d collected chunks for session %s.",
                len(chunks),
                self.session_id,
            )
            for chunk in chunks:
                self._delete_chunk_file(chunk)
            return

        segment_index = self._segment_index + 1
        start_sec = min(chunk.window_start_sec for chunk in chunks)
        end_sec = max(chunk.window_end_sec for chunk in chunks)
        stem = f"segment-{segment_index:03d}-{start_sec:.3f}-{end_sec:.3f}"
        wav_path = self.output_dir / f"{stem}.wav"
        metadata_path = self.output_dir / f"{stem}.json"

        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            _write_wav(wav_path, merged)
            metadata_path.write_text(
                json.dumps(
                    self._segment_metadata(
                        chunks,
                        segment_index=segment_index,
                        start_sec=start_sec,
                        end_sec=end_sec,
                        sample_rate=merged.framerate,
                        audio_filename=wav_path.name,
                        reason=reason,
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            logger.exception(
                "Failed to write collected segment %s for session %s.",
                stem,
                self.session_id,
            )
            wav_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            for chunk in chunks:
                self._delete_chunk_file(chunk)
            return

        for chunk in chunks:
            self._delete_chunk_file(chunk)

        self._segment_index = segment_index
        self._segments.append(
            CollectedSegmentSummary(
                segment_index=segment_index,
                start_sec=round(start_sec, 3),
                end_sec=round(end_sec, 3),
                duration_sec=round(end_sec - start_sec, 3),
                event_count=sum(len(chunk.events) for chunk in chunks),
                labels=_sorted_unique_labels(chunks),
                audio_filename=wav_path.name,
                metadata_filename=metadata_path.name,
            )
        )
        if self.mp3_scheduler is not None:
            try:
                self.mp3_scheduler(wav_path)
            except Exception:
                logger.exception("Could not schedule MP3 conversion for %s.", wav_path)

    def _segment_metadata(
        self,
        chunks: list[_ChunkEntry],
        *,
        segment_index: int,
        start_sec: float,
        end_sec: float,
        sample_rate: int,
        audio_filename: str,
        reason: str,
    ) -> dict:
        return {
            "session_id": self.session_id,
            "segment_index": segment_index,
            "start_sec": round(start_sec, 3),
            "end_sec": round(end_sec, 3),
            "duration_sec": round(end_sec - start_sec, 3),
            "sample_rate": sample_rate,
            "audio_filename": audio_filename,
            "finalize_reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "chunk_sequence_ids": [chunk.sequence_id for chunk in chunks],
            "events": [
                {**event.model_dump(), "source_sequence_id": chunk.sequence_id}
                for chunk in chunks
                for event in chunk.events
            ],
        }

    def _write_session_summary(self, summary: LiveSessionEndResponse) -> None:
        if not self._segments:
            return
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "session.json").write_text(
                json.dumps(
                    {
                        **summary.model_dump(),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            logger.exception(
                "Failed to write session summary for session %s.", self.session_id
            )

    def _delete_chunk_file(self, entry: _ChunkEntry) -> None:
        try:
            entry.wav_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to delete live chunk %s.", entry.wav_path)

    def _cleanup_source_dirs(self) -> None:
        for source_dir in self._source_dirs:
            try:
                source_dir.rmdir()
            except OSError:
                # Directory not empty (legacy debug files) or already gone.
                continue


@dataclass(frozen=True)
class _MergedAudio:
    nchannels: int
    sampwidth: int
    framerate: int
    frames: bytes


def _merge_chunk_audio(chunks: list[_ChunkEntry]) -> _MergedAudio:
    """Concatenate overlapping WAV windows into one continuous PCM stream.

    Windows overlap (2 s window, 1 s hop), so each chunk after the first
    only contributes the frames past the previous max window end. Chunks
    with mismatched WAV params or unreadable data are skipped, not fatal.
    """
    merged: bytearray | None = None
    params: tuple[int, int, int] | None = None
    prev_end_sec = 0.0

    for chunk in chunks:
        try:
            with wave.open(str(chunk.wav_path), "rb") as reader:
                chunk_params = (
                    reader.getnchannels(),
                    reader.getsampwidth(),
                    reader.getframerate(),
                )
                frames = reader.readframes(reader.getnframes())
        except (OSError, wave.Error, EOFError):
            logger.exception("Skipping unreadable live chunk %s.", chunk.wav_path)
            continue

        if merged is None:
            merged = bytearray(frames)
            params = chunk_params
            prev_end_sec = chunk.window_end_sec
            continue

        if chunk_params != params:
            logger.warning(
                "Skipping live chunk %s with mismatched WAV params %s (expected %s).",
                chunk.wav_path,
                chunk_params,
                params,
            )
            continue

        nchannels, sampwidth, framerate = params
        bytes_per_frame = nchannels * sampwidth
        overlap_sec = prev_end_sec - chunk.window_start_sec
        skip_bytes = max(0, round(overlap_sec * framerate)) * bytes_per_frame
        if skip_bytes < len(frames):
            merged.extend(frames[skip_bytes:])
        prev_end_sec = max(prev_end_sec, chunk.window_end_sec)

    if merged is None or params is None:
        raise ValueError("No readable audio chunks to merge.")
    nchannels, sampwidth, framerate = params
    return _MergedAudio(
        nchannels=nchannels,
        sampwidth=sampwidth,
        framerate=framerate,
        frames=bytes(merged),
    )


def _write_wav(path: Path, audio: _MergedAudio) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(audio.nchannels)
        writer.setsampwidth(audio.sampwidth)
        writer.setframerate(audio.framerate)
        writer.writeframes(audio.frames)


def _sorted_unique_labels(chunks: list[_ChunkEntry]) -> list[str]:
    return sorted({event.label for chunk in chunks for event in chunk.events})


class LiveCollectionManager:
    """Tracks one SegmentCollector per live session across requests."""

    def __init__(self, stale_session_sec: float = DEFAULT_STALE_SESSION_SEC):
        self.stale_session_sec = stale_session_sec
        self._lock = Lock()
        self._collectors: dict[str, SegmentCollector] = {}

    def add_chunk(
        self,
        session_id: str,
        *,
        output_dir: Path,
        policy: CollectionPolicy,
        mp3_scheduler: Callable[[Path], object] | None = None,
        sequence_id: int,
        window_start_sec: float,
        window_end_sec: float,
        wav_path: Path,
        events: Sequence[SoundEvent],
    ) -> str:
        with self._lock:
            collector = self._collectors.get(session_id)
            if collector is None:
                collector = SegmentCollector(
                    session_id,
                    output_dir,
                    policy,
                    mp3_scheduler=mp3_scheduler,
                )
                self._collectors[session_id] = collector
            stale = self._pop_stale_collectors(exclude=session_id)
        for stale_collector in stale:
            stale_collector.end_session()
        return collector.add_chunk(
            sequence_id=sequence_id,
            window_start_sec=window_start_sec,
            window_end_sec=window_end_sec,
            wav_path=wav_path,
            events=events,
        )

    def end_session(self, session_id: str) -> LiveSessionEndResponse:
        with self._lock:
            collector = self._collectors.pop(session_id, None)
        if collector is None:
            return LiveSessionEndResponse(
                session_id=session_id,
                segment_count=0,
                total_collected_duration_sec=0.0,
                kept_chunk_count=0,
                discarded_silent_chunk_count=0,
                discarded_speech_chunk_count=0,
                segments=[],
            )
        return collector.end_session()

    def _pop_stale_collectors(self, exclude: str) -> list[SegmentCollector]:
        now = monotonic()
        stale_ids = [
            session_id
            for session_id, collector in self._collectors.items()
            if session_id != exclude
            and now - collector.last_activity_monotonic > self.stale_session_sec
        ]
        return [self._collectors.pop(session_id) for session_id in stale_ids]
