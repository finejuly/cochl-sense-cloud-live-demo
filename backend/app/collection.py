from __future__ import annotations

import json
import logging
import shutil
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
    CollectedSessionInfo,
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
    min_segment_sec: float = 5.0
    max_segment_sec: float = 20.0
    reorder_hold_back_sec: float = DEFAULT_REORDER_HOLD_BACK_SEC


def policy_from_settings(settings: Settings) -> CollectionPolicy:
    return CollectionPolicy(
        confidence_threshold=settings.collection_confidence_threshold,
        exclude_label_keywords=settings.collection_exclude_label_keywords,
        min_segment_sec=settings.collection_min_segment_sec,
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
        session_name: str | None = None,
    ):
        self.session_id = session_id
        self.output_dir = output_dir
        self.policy = policy
        self.mp3_scheduler = mp3_scheduler
        self.session_name = session_name
        self.started_at = datetime.now(timezone.utc)
        self.last_activity_monotonic = monotonic()
        self._lock = Lock()
        self._pending: list[_ChunkEntry] = []
        self._max_window_end_seen = 0.0
        self._segment_chunks: list[_ChunkEntry] = []
        self._context_buffer: list[_ChunkEntry] = []
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
            self._flush_context_buffer()
            summary = LiveSessionEndResponse(
                session_id=self.session_id,
                session_name=self.session_name,
                started_at=self.started_at.isoformat(),
                ended_at=datetime.now(timezone.utc).isoformat(),
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
        if entry.decision == CHUNK_DISCARDED_SPEECH:
            self._discarded_speech_count += 1
            self._delete_chunk_file(entry)
            # Never let one collected file span across a speech region, and
            # never reuse audio adjacent to it as context for later segments.
            self._flush_context_buffer()
            self._finalize_current_segment("speech_boundary")
            return
        if entry.decision == CHUNK_DISCARDED_SILENT:
            self._handle_silent_entry(entry)
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
        if self._segment_chunks:
            self._segment_chunks.append(entry)
        else:
            self._start_segment_with_context(entry)

    def _handle_silent_entry(self, entry: _ChunkEntry) -> None:
        """Silent chunks are kept around as background context, not just dropped.

        A contiguous silent chunk extends an open segment that is still below
        min_segment_sec (trailing padding); otherwise it waits in the context
        buffer so it can become pre-roll for the next meaningful segment.
        """
        if self._segment_chunks:
            current_end = max(chunk.window_end_sec for chunk in self._segment_chunks)
            segment_start = min(chunk.window_start_sec for chunk in self._segment_chunks)
            if (
                entry.window_start_sec <= current_end + _TIME_EPSILON_SEC
                and current_end - segment_start
                < self.policy.min_segment_sec - _TIME_EPSILON_SEC
                and entry.window_end_sec - segment_start
                <= self.policy.max_segment_sec + _TIME_EPSILON_SEC
            ):
                self._segment_chunks.append(entry)
                return
        self._push_context(entry)

    def _push_context(self, entry: _ChunkEntry) -> None:
        if (
            self._context_buffer
            and entry.window_start_sec
            > self._context_buffer[-1].window_end_sec + _TIME_EPSILON_SEC
        ):
            # The chain broke, so the buffered audio can never touch a future
            # segment start — discard it.
            self._flush_context_buffer()
        self._context_buffer.append(entry)
        while self._context_buffer and (
            self._context_buffer[0].window_end_sec
            < entry.window_end_sec - self.policy.min_segment_sec - _TIME_EPSILON_SEC
        ):
            dropped = self._context_buffer.pop(0)
            self._discarded_silent_count += 1
            self._delete_chunk_file(dropped)

    def _start_segment_with_context(self, entry: _ChunkEntry) -> None:
        # Split the min-length deficit across both sides: only half becomes
        # leading pre-roll here, so trailing silence can fill the other half
        # and the detection sits roughly centered. If the trailing side turns
        # out short, _finalize_current_segment tops up from the leftovers.
        entry_duration = entry.window_end_sec - entry.window_start_sec
        lead_limit = max(0.0, (self.policy.min_segment_sec - entry_duration) / 2)
        self._segment_chunks = self._prepend_context([entry], lead_limit=lead_limit)

    def _prepend_context(
        self,
        chunks: list[_ChunkEntry],
        lead_limit: float | None = None,
    ) -> list[_ChunkEntry]:
        segment_start = min(chunk.window_start_sec for chunk in chunks)
        segment_end = max(chunk.window_end_sec for chunk in chunks)
        anchor_start = segment_start
        while self._context_buffer:
            if (
                segment_end - segment_start
                >= self.policy.min_segment_sec - _TIME_EPSILON_SEC
            ):
                break
            if (
                lead_limit is not None
                and anchor_start - segment_start >= lead_limit - _TIME_EPSILON_SEC
            ):
                break
            candidate = self._context_buffer[-1]
            if candidate.window_start_sec >= segment_start - _TIME_EPSILON_SEC:
                if candidate.window_end_sec <= segment_end + _TIME_EPSILON_SEC:
                    # Fully covered by audio the segment already has.
                    self._context_buffer.pop()
                    self._discarded_silent_count += 1
                    self._delete_chunk_file(candidate)
                    continue
                # Context past the segment — pre-roll for the next one.
                break
            if candidate.window_end_sec + _TIME_EPSILON_SEC < segment_start:
                break
            if (
                segment_end - candidate.window_start_sec
                > self.policy.max_segment_sec + _TIME_EPSILON_SEC
            ):
                break
            self._context_buffer.pop()
            chunks.insert(0, candidate)
            segment_start = candidate.window_start_sec
        return chunks

    def _flush_context_buffer(self) -> None:
        for chunk in self._context_buffer:
            self._discarded_silent_count += 1
            self._delete_chunk_file(chunk)
        self._context_buffer = []

    def _finalize_current_segment(self, reason: str) -> None:
        chunks = self._segment_chunks
        self._segment_chunks = []
        if not chunks:
            return
        # Trailing silence may have been too short — top the segment up to the
        # minimum length from leftover leading context.
        chunks = self._prepend_context(chunks)

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
                labels=_sorted_unique_labels(chunks, self.policy.confidence_threshold),
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
            "session_name": self.session_name,
            "session_started_at": self.started_at.isoformat(),
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


def _sorted_unique_labels(chunks: list[_ChunkEntry], confidence_threshold: float) -> list[str]:
    return sorted(
        {
            event.label
            for chunk in chunks
            for event in chunk.events
            if event.confidence is None or event.confidence >= confidence_threshold
        }
    )


class LiveCollectionManager:
    """Tracks one SegmentCollector per live session across requests.

    Ended sessions leave a tombstone so chunks whose analyses complete after
    `end_session` are deleted immediately instead of respawning a collector
    that would strand files in `recordings/live/`.
    """

    def __init__(self, stale_session_sec: float = DEFAULT_STALE_SESSION_SEC):
        self.stale_session_sec = stale_session_sec
        self._lock = Lock()
        self._collectors: dict[str, SegmentCollector] = {}
        self._ended_sessions: dict[str, float] = {}

    def add_chunk(
        self,
        session_id: str,
        *,
        output_dir: Path,
        policy: CollectionPolicy,
        mp3_scheduler: Callable[[Path], object] | None = None,
        session_name: str | None = None,
        sequence_id: int,
        window_start_sec: float,
        window_end_sec: float,
        wav_path: Path,
        events: Sequence[SoundEvent],
    ) -> str:
        with self._lock:
            self._prune_tombstones()
            if session_id in self._ended_sessions:
                _discard_late_chunk(wav_path)
                return classify_chunk_events(events, policy)
            collector = self._collectors.get(session_id)
            if collector is None:
                collector = SegmentCollector(
                    session_id,
                    output_dir,
                    policy,
                    mp3_scheduler=mp3_scheduler,
                    session_name=session_name,
                )
                self._collectors[session_id] = collector
            elif collector.session_name is None and session_name:
                collector.session_name = session_name
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

    def end_session(
        self,
        session_id: str,
        session_name: str | None = None,
    ) -> LiveSessionEndResponse:
        with self._lock:
            self._prune_tombstones()
            self._ended_sessions[session_id] = monotonic()
            collector = self._collectors.pop(session_id, None)
        if collector is None:
            return LiveSessionEndResponse(
                session_id=session_id,
                session_name=session_name,
                segment_count=0,
                total_collected_duration_sec=0.0,
                kept_chunk_count=0,
                discarded_silent_chunk_count=0,
                discarded_speech_chunk_count=0,
                segments=[],
            )
        if collector.session_name is None and session_name:
            collector.session_name = session_name
        return collector.end_session()

    def _pop_stale_collectors(self, exclude: str) -> list[SegmentCollector]:
        now = monotonic()
        stale_ids = [
            session_id
            for session_id, collector in self._collectors.items()
            if session_id != exclude
            and now - collector.last_activity_monotonic > self.stale_session_sec
        ]
        for session_id in stale_ids:
            self._ended_sessions[session_id] = now
        return [self._collectors.pop(session_id) for session_id in stale_ids]

    def _prune_tombstones(self) -> None:
        now = monotonic()
        expired = [
            session_id
            for session_id, ended_at in self._ended_sessions.items()
            if now - ended_at > self.stale_session_sec
        ]
        for session_id in expired:
            del self._ended_sessions[session_id]


def _discard_late_chunk(wav_path: Path) -> None:
    try:
        wav_path.unlink(missing_ok=True)
        wav_path.parent.rmdir()
    except OSError:
        # Parent not empty or already gone — nothing else to clean.
        pass


def safe_collected_session_dir(collected_root: Path, session_id: str) -> Path | None:
    """Resolves a session directory strictly one level under collected_root."""
    root = collected_root.resolve()
    target = (root / session_id).resolve()
    if target.parent != root or not target.is_dir():
        return None
    return target


def list_collected_sessions(collected_root: Path) -> list[CollectedSessionInfo]:
    if not collected_root.is_dir():
        return []
    sessions = [
        info
        for session_dir in collected_root.iterdir()
        if session_dir.is_dir()
        and (info := _load_collected_session(session_dir)) is not None
    ]
    sessions.sort(key=lambda session: session.started_at or "", reverse=True)
    return sessions


def _load_collected_session(session_dir: Path) -> CollectedSessionInfo | None:
    segments: list[CollectedSegmentSummary] = []
    for metadata_path in sorted(session_dir.glob("segment-*.json")):
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Skipping unreadable segment metadata %s.", metadata_path)
            continue
        audio_filename = _resolve_segment_audio(session_dir, metadata_path.stem)
        if audio_filename is None:
            continue
        events = data.get("events") or []
        segments.append(
            CollectedSegmentSummary(
                segment_index=int(data.get("segment_index") or len(segments) + 1),
                start_sec=float(data.get("start_sec") or 0.0),
                end_sec=float(data.get("end_sec") or 0.0),
                duration_sec=float(data.get("duration_sec") or 0.0),
                event_count=len(events),
                labels=sorted(
                    {
                        str(event.get("label"))
                        for event in events
                        if isinstance(event, dict) and event.get("label")
                    }
                ),
                audio_filename=audio_filename,
                metadata_filename=metadata_path.name,
            )
        )
    if not segments:
        return None

    session_name = started_at = ended_at = None
    session_json = session_dir / "session.json"
    if session_json.is_file():
        try:
            payload = json.loads(session_json.read_text(encoding="utf-8"))
            session_name = payload.get("session_name")
            started_at = payload.get("started_at")
            ended_at = payload.get("ended_at")
        except (OSError, json.JSONDecodeError):
            logger.warning("Skipping unreadable session summary %s.", session_json)

    return CollectedSessionInfo(
        session_id=session_dir.name,
        session_name=session_name,
        started_at=started_at,
        ended_at=ended_at,
        segment_count=len(segments),
        total_collected_duration_sec=round(
            sum(segment.duration_sec for segment in segments), 3
        ),
        segments=segments,
    )


def _resolve_segment_audio(session_dir: Path, stem: str) -> str | None:
    for suffix in (".mp3", ".wav"):
        if (session_dir / f"{stem}{suffix}").is_file():
            return f"{stem}{suffix}"
    return None


def delete_collected_session(collected_root: Path, session_id: str) -> bool:
    session_dir = safe_collected_session_dir(collected_root, session_id)
    if session_dir is None:
        return False
    shutil.rmtree(session_dir, ignore_errors=True)
    return True


def delete_collected_segment(
    collected_root: Path,
    session_id: str,
    filename: str,
) -> bool:
    session_dir = safe_collected_session_dir(collected_root, session_id)
    if session_dir is None or Path(filename).name != filename:
        return False
    stem = Path(filename).stem
    if not stem.startswith("segment-"):
        return False
    deleted = False
    for suffix in (".wav", ".mp3", ".json"):
        target = session_dir / f"{stem}{suffix}"
        if target.is_file():
            target.unlink(missing_ok=True)
            deleted = True
    if deleted and not any(session_dir.glob("segment-*.json")):
        # Last segment removed — drop the now-empty session directory.
        shutil.rmtree(session_dir, ignore_errors=True)
    return deleted
