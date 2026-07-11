from __future__ import annotations

import json
import logging
import re
import shutil
import wave
from bisect import insort
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from threading import Event, Lock
from time import monotonic

from backend.app.config import Settings
from backend.app.curation import (
    CandidateFeatures,
    CurationDecision,
    CurationPolicy,
    ObservedEvent,
    SegmentCurator,
    build_candidate_features,
)
from backend.app.gcs_upload import UPLOAD_MARKER_FILENAME
from backend.app.models import (
    CollectedSegmentSummary,
    CollectedSessionInfo,
    GcsUploadStatus,
    LiveCurationProgress,
    LiveSessionEndResponse,
    SoundEvent,
)
from backend.app.segment_files import (
    make_segment_stem,
    resolve_segment_audio,
    sorted_segment_metadata_paths,
)

logger = logging.getLogger(__name__)

CHUNK_COLLECTED = "collected"
CHUNK_DISCARDED_SILENT = "discarded_silent"
CHUNK_DISCARDED_SPEECH = "discarded_speech"
CHUNK_DISCARDED_LATE = "discarded_late"

DEFAULT_REORDER_HOLD_BACK_SEC = 6.0
DEFAULT_STALE_SESSION_SEC = 600.0
_TIME_EPSILON_SEC = 1e-3
STALE_UPLOAD_MARKER_FILENAME = ".gcs-upload.stale.json"
CLOSED_SESSION_MARKER_FILENAME = ".session-closed.json"
_SEGMENT_DELETE_TOMBSTONE_SUFFIX = ".deleted"
_segment_file_lock = Lock()

_PRIVACY_TOKEN_ALIASES: dict[str, frozenset[str]] = {
    "speech": frozenset({"speech", "speeches"}),
    "whisper": frozenset(
        {"whisper", "whispers", "whispered", "whispering", "whisperer"}
    ),
    "sing": frozenset({"sing", "sings", "sang", "sung", "singing", "singer"}),
    "conversation": frozenset(
        {"conversation", "conversations", "conversational"}
    ),
    "narration": frozenset(
        {"narration", "narrations", "narrating", "narrator", "narrators"}
    ),
    "talk": frozenset(
        {"talk", "talks", "talked", "talking", "talker", "talkers"}
    ),
}


@dataclass(frozen=True)
class CollectionPolicy:
    confidence_threshold: float = 0.5
    exclude_label_keywords: tuple[str, ...] = ()
    min_segment_sec: float = 5.0
    max_segment_sec: float = 20.0
    silence_close_sec: float = 3.0
    reorder_hold_back_sec: float = DEFAULT_REORDER_HOLD_BACK_SEC
    curation: CurationPolicy = field(default_factory=CurationPolicy)


def policy_from_settings(settings: Settings) -> CollectionPolicy:
    return CollectionPolicy(
        confidence_threshold=settings.collection_confidence_threshold,
        exclude_label_keywords=settings.collection_exclude_label_keywords,
        min_segment_sec=settings.collection_min_segment_sec,
        max_segment_sec=settings.collection_max_segment_sec,
        silence_close_sec=settings.collection_silence_close_sec,
        curation=CurationPolicy(
            max_segments=settings.collection_max_selected_segments,
            max_duration_sec=settings.collection_max_selected_duration_sec,
            max_audio_bytes=settings.collection_max_selected_audio_mb * 1024 * 1024,
            repeat_cooldown_sec=settings.collection_repeat_cooldown_sec,
            max_quota_label_share=settings.collection_max_quota_label_share,
        ),
    )


def is_privacy_sensitive_label(label: str, exclude_keywords: Sequence[str]) -> bool:
    """Match privacy labels on taxonomy tokens, never arbitrary substrings.

    Cochl labels commonly use underscores (for example ``Male_speech``), while
    a few speech categories are inflected words (``Whispering``/``Singing``).
    Token matching plus a deliberately small alias table keeps those categories
    private without treating unrelated labels such as ``Reversing_beep`` as
    singing merely because their spelling contains ``sing``.
    """
    label_tokens = tuple(re.findall(r"[a-z0-9]+", label.casefold()))
    if not label_tokens:
        return False

    for raw_keyword in exclude_keywords:
        keyword_tokens = tuple(re.findall(r"[a-z0-9]+", raw_keyword.casefold()))
        if not keyword_tokens:
            continue
        if len(keyword_tokens) == 1:
            variants = _PRIVACY_TOKEN_ALIASES.get(
                keyword_tokens[0], frozenset(keyword_tokens)
            )
            if any(token in variants for token in label_tokens):
                return True
            continue
        width = len(keyword_tokens)
        if any(
            label_tokens[index : index + width] == keyword_tokens
            for index in range(len(label_tokens) - width + 1)
        ):
            return True
    return False


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
        self._processed_frontier: tuple[float, int] | None = None
        self._segment_chunks: list[_ChunkEntry] = []
        self._context_buffer: list[_ChunkEntry] = []
        self._segment_index = 0
        self._candidate_index = 0
        self._segments: list[CollectedSegmentSummary] = []
        self._curator = SegmentCurator(policy.curation)
        self._kept_chunk_count = 0
        self._discarded_silent_count = 0
        self._discarded_speech_count = 0
        self._source_dirs: set[Path] = set()
        self._ended = False
        self._ended_summary: LiveSessionEndResponse | None = None

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
                _discard_late_chunk(entry.wav_path)
                return CHUNK_DISCARDED_LATE
            self.last_activity_monotonic = monotonic()
            self._source_dirs.add(wav_path.parent)
            entry_order = (entry.window_start_sec, entry.sequence_id)
            if (
                self._processed_frontier is not None
                and entry_order <= self._processed_frontier
            ):
                logger.warning(
                    "Discarding late chunk %s for session %s: order %s is not "
                    "after processed frontier %s.",
                    wav_path,
                    self.session_id,
                    entry_order,
                    self._processed_frontier,
                )
                _discard_late_chunk(entry.wav_path)
                return CHUNK_DISCARDED_LATE
            insort(self._pending, entry)
            self._max_window_end_seen = max(self._max_window_end_seen, window_end_sec)
            watermark = self._max_window_end_seen - self.policy.reorder_hold_back_sec
            while self._pending and self._pending[0].window_start_sec <= watermark:
                self._process_entry(self._pending.pop(0))
        return decision

    def end_session(self) -> LiveSessionEndResponse:
        with self._lock:
            if self._ended_summary is not None:
                return self._ended_summary
            self._ended = True
            while self._pending:
                self._process_entry(self._pending.pop(0))
            self._finalize_current_segment("session_end")
            self._flush_context_buffer()
            summary = self._build_summary(ended=True)
            # The client must not be told that a session is complete until the
            # terminal state is durably visible to a restarted process.  Live
            # progress snapshots are best-effort, but the final snapshot is a
            # commit point and therefore propagates write failures.
            self._write_session_summary(summary, required=True)
            self._cleanup_source_dirs()
            self._ended_summary = summary
        return summary

    def _build_summary(self, *, ended: bool) -> LiveSessionEndResponse:
        curation = self._curator.summary()
        return LiveSessionEndResponse(
            session_id=self.session_id,
            session_name=self.session_name,
            started_at=self.started_at.isoformat(),
            ended_at=datetime.now(timezone.utc).isoformat() if ended else None,
            segment_count=len(self._segments),
            total_collected_duration_sec=round(
                sum(segment.duration_sec for segment in self._segments), 3
            ),
            kept_chunk_count=self._kept_chunk_count,
            discarded_silent_chunk_count=self._discarded_silent_count,
            discarded_speech_chunk_count=self._discarded_speech_count,
            segments=list(self._segments),
            **curation.__dict__,
        )

    def curation_progress(self) -> LiveCurationProgress:
        with self._lock:
            curation = self._curator.summary()
            return LiveCurationProgress(
                candidate_segment_count=curation.candidate_segment_count,
                selected_segment_count=len(self._segments),
                rejected_repetitive_count=curation.rejected_repetitive_count,
                rejected_class_balance_count=curation.rejected_class_balance_count,
                rejected_session_budget_count=(
                    curation.rejected_session_budget_count
                ),
                invalid_audio_count=curation.invalid_audio_count,
                write_error_count=curation.write_error_count,
            )

    def _process_entry(self, entry: _ChunkEntry) -> None:
        entry_order = (entry.window_start_sec, entry.sequence_id)
        if (
            self._processed_frontier is not None
            and entry_order <= self._processed_frontier
        ):
            logger.error(
                "Discarding internally out-of-order chunk %s for session %s: "
                "order %s is not after processed frontier %s.",
                entry.wav_path,
                self.session_id,
                entry_order,
                self._processed_frontier,
            )
            self._delete_chunk_file(entry)
            return
        self._processed_frontier = entry_order
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
            if (
                entry.window_end_sec - segment_start
                > self.policy.max_segment_sec + _TIME_EPSILON_SEC
            ):
                self._finalize_current_segment(
                    "max_duration",
                    clip_end_sec=entry.window_start_sec,
                )
            elif (
                entry.window_start_sec > current_end + _TIME_EPSILON_SEC
                and not self._bridge_context_to(entry)
            ):
                self._finalize_current_segment("gap")
        if self._segment_chunks:
            self._segment_chunks.append(entry)
        else:
            self._start_segment_with_context(entry)

    def _handle_silent_entry(self, entry: _ChunkEntry) -> None:
        """Silent chunks are kept around as background context, not just dropped.

        A contiguous silent chunk extends an open segment that is still below
        min_segment_sec (trailing padding). Once silence stretches
        silence_close_sec past the last meaningful chunk, the open segment is
        finalized right away so its file appears while recording continues.
        Otherwise the chunk waits in the context buffer so it can become
        pre-roll for the next meaningful segment.
        """
        if self._segment_chunks:
            current_end = max(chunk.window_end_sec for chunk in self._segment_chunks)
            segment_start = min(chunk.window_start_sec for chunk in self._segment_chunks)
            contiguous = entry.window_start_sec <= current_end + _TIME_EPSILON_SEC
            if (
                contiguous
                and current_end - segment_start
                < self.policy.min_segment_sec - _TIME_EPSILON_SEC
                and entry.window_end_sec - segment_start
                <= self.policy.max_segment_sec + _TIME_EPSILON_SEC
            ):
                self._segment_chunks.append(entry)
                last_kept_end = max(
                    chunk.window_end_sec
                    for chunk in self._segment_chunks
                    if chunk.decision == CHUNK_COLLECTED
                )
                padded_end = max(
                    chunk.window_end_sec for chunk in self._segment_chunks
                )
                padded_start = min(
                    chunk.window_start_sec for chunk in self._segment_chunks
                )
                if (
                    padded_end - padded_start
                    >= self.policy.min_segment_sec - _TIME_EPSILON_SEC
                    and entry.window_end_sec - last_kept_end
                    >= self.policy.silence_close_sec - _TIME_EPSILON_SEC
                ):
                    # The same chunk can satisfy both minimum padding and the
                    # close threshold. Decide now instead of waiting for one
                    # redundant future window.
                    self._finalize_current_segment("silence")
                return
            last_kept_end = max(
                chunk.window_end_sec
                for chunk in self._segment_chunks
                if chunk.decision == CHUNK_COLLECTED
            )
            if (
                contiguous
                and entry.window_end_sec - last_kept_end
                >= self.policy.silence_close_sec - _TIME_EPSILON_SEC
            ):
                # The sound has been over for a while — write the file now
                # instead of waiting for the next event or the session end.
                self._finalize_current_segment("silence")
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
        context_retention_sec = max(
            self.policy.min_segment_sec,
            self.policy.silence_close_sec,
        )
        while self._context_buffer and (
            self._context_buffer[0].window_end_sec
            < entry.window_end_sec - context_retention_sec - _TIME_EPSILON_SEC
        ):
            dropped = self._context_buffer.pop(0)
            self._discarded_silent_count += 1
            self._delete_chunk_file(dropped)

    def _bridge_context_to(self, entry: _ChunkEntry) -> bool:
        """Move buffered silence into an open segment when it reaches `entry`.

        Once a segment reaches its minimum length, later silent chunks wait in
        the context buffer instead of extending the file. A nearby detection
        should consume that buffer and remain in the same segment; only a real
        hole in the captured audio should force a gap split.
        """
        if not self._segment_chunks or not self._context_buffer:
            return False

        bridged_end = max(chunk.window_end_sec for chunk in self._segment_chunks)
        for candidate in self._context_buffer:
            if candidate.window_start_sec > bridged_end + _TIME_EPSILON_SEC:
                return False
            bridged_end = max(bridged_end, candidate.window_end_sec)

        if entry.window_start_sec > bridged_end + _TIME_EPSILON_SEC:
            return False

        self._segment_chunks.extend(self._context_buffer)
        self._context_buffer = []
        return True

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

    def _finalize_current_segment(
        self,
        reason: str,
        *,
        clip_end_sec: float | None = None,
    ) -> None:
        chunks = self._segment_chunks
        self._segment_chunks = []
        if not chunks:
            return
        # Trailing silence may have been too short — top the segment up to the
        # minimum length from leftover leading context.
        chunks = self._prepend_context(chunks)

        self._candidate_index += 1
        candidate_id = self._candidate_index
        start_sec = min(chunk.window_start_sec for chunk in chunks)
        end_sec = min(
            max(chunk.window_end_sec for chunk in chunks),
            clip_end_sec if clip_end_sec is not None else float("inf"),
        )
        merge_result = _merge_chunk_audio(chunks, clip_end_sec=clip_end_sec)
        if merge_result.audio is None or merge_result.skipped_sequence_ids:
            self._curator.record_invalid_audio(
                candidate_id,
                start_sec,
                end_sec,
                merge_result.skipped_sequence_ids,
            )
            self._append_decision_record(
                {
                    "candidate_id": candidate_id,
                    "start_sec": round(start_sec, 3),
                    "end_sec": round(end_sec, 3),
                    "selected": False,
                    "reason": "invalid_audio",
                    "skipped_sequence_ids": list(merge_result.skipped_sequence_ids),
                    "policy_version": self.policy.curation.policy_version,
                }
            )
            self._delete_chunks(chunks)
            self._write_session_summary(self._build_summary(ended=False))
            return

        merged = merge_result.audio
        duration_sec = len(merged.frames) / (
            merged.nchannels * merged.sampwidth * merged.framerate
        )
        observations = self._observed_events(
            chunks,
            consumed_sequence_ids=merge_result.consumed_sequence_ids,
            clip_end_sec=clip_end_sec,
        )
        try:
            candidate = build_candidate_features(
                candidate_id=candidate_id,
                start_sec=start_sec,
                end_sec=end_sec,
                duration_sec=duration_sec,
                estimated_audio_bytes=len(merged.frames) + 44,
                observations=observations,
                confidence_threshold=self.policy.confidence_threshold,
            )
        except ValueError:
            logger.exception(
                "Candidate %d for session %s has no usable event tracks.",
                candidate_id,
                self.session_id,
            )
            self._curator.record_invalid_audio(candidate_id, start_sec, end_sec, ())
            self._append_decision_record(
                {
                    "candidate_id": candidate_id,
                    "start_sec": round(start_sec, 3),
                    "end_sec": round(end_sec, 3),
                    "selected": False,
                    "reason": "invalid_audio",
                    "skipped_sequence_ids": [],
                    "policy_version": self.policy.curation.policy_version,
                }
            )
            self._delete_chunks(chunks)
            self._write_session_summary(self._build_summary(ended=False))
            return

        decision = self._curator.evaluate(candidate)
        if not decision.selected:
            self._curator.record_rejected(candidate, decision)
            self._append_decision_record(
                self._decision_record(candidate, decision)
            )
            self._delete_chunks(chunks)
            self._write_session_summary(self._build_summary(ended=False))
            return

        segment_index = self._segment_index + 1
        stem = make_segment_stem(segment_index, start_sec, end_sec)
        wav_path = self.output_dir / f"{stem}.wav"
        metadata_path = self.output_dir / f"{stem}.json"
        temporary_wav = self.output_dir / f".{stem}.wav.tmp"
        temporary_metadata = self.output_dir / f".{stem}.json.tmp"

        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            _write_wav(temporary_wav, merged)
            temporary_metadata.write_text(
                json.dumps(
                    self._segment_metadata(
                        candidate,
                        decision,
                        segment_index=segment_index,
                        sample_rate=merged.framerate,
                        audio_filename=wav_path.name,
                        reason=reason,
                        chunk_sequence_ids=merge_result.consumed_sequence_ids,
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary_wav.replace(wav_path)
            temporary_metadata.replace(metadata_path)
        except OSError:
            logger.exception(
                "Failed to write collected segment %s for session %s.",
                stem,
                self.session_id,
            )
            temporary_wav.unlink(missing_ok=True)
            temporary_metadata.unlink(missing_ok=True)
            wav_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            self._curator.record_write_error(candidate)
            self._append_decision_record(
                self._decision_record(candidate, decision, reason="write_error")
            )
            self._delete_chunks(chunks)
            self._write_session_summary(self._build_summary(ended=False))
            return

        self._curator.commit_selected(candidate, decision)
        self._delete_chunks(chunks)
        self._segment_index = segment_index
        self._segments.append(
            CollectedSegmentSummary(
                segment_index=segment_index,
                start_sec=round(start_sec, 3),
                end_sec=round(end_sec, 3),
                duration_sec=round(duration_sec, 3),
                event_count=len(candidate.tracks),
                labels=sorted({track.label for track in candidate.tracks}),
                audio_filename=wav_path.name,
                metadata_filename=metadata_path.name,
                primary_label=candidate.primary_label,
                quota_label=decision.quota_label,
                selection_reason=decision.reason,
            )
        )
        if self.mp3_scheduler is not None:
            try:
                self.mp3_scheduler(wav_path)
            except Exception:
                logger.exception("Could not schedule MP3 conversion for %s.", wav_path)
        self._write_session_summary(self._build_summary(ended=False))

    def _segment_metadata(
        self,
        candidate: CandidateFeatures,
        decision: CurationDecision,
        *,
        segment_index: int,
        sample_rate: int,
        audio_filename: str,
        reason: str,
        chunk_sequence_ids: tuple[int, ...],
    ) -> dict:
        return {
            "session_id": self.session_id,
            "session_name": self.session_name,
            "session_started_at": self.started_at.isoformat(),
            "segment_index": segment_index,
            "start_sec": round(candidate.start_sec, 3),
            "end_sec": round(candidate.end_sec, 3),
            "duration_sec": round(candidate.duration_sec, 3),
            "sample_rate": sample_rate,
            "audio_filename": audio_filename,
            "finalize_reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "chunk_sequence_ids": list(chunk_sequence_ids),
            "events": [
                {
                    "start_time_sec": round(track.start_sec, 3),
                    "end_time_sec": round(track.end_sec, 3),
                    "label": track.label,
                    "confidence": track.max_confidence,
                    "supporting_window_count": track.supporting_window_count,
                }
                for track in candidate.tracks
            ],
            "curation": {
                "policy_version": decision.policy_version,
                "candidate_id": candidate.candidate_id,
                "signature": list(candidate.signature),
                "primary_label": candidate.primary_label,
                "quota_label": decision.quota_label,
                "selection_reason": decision.reason,
            },
        }

    def _write_session_summary(
        self,
        summary: LiveSessionEndResponse,
        *,
        required: bool = False,
    ) -> None:
        if summary.candidate_segment_count == 0 and not summary.segments:
            if required:
                self._write_closed_session_marker(summary)
            return
        temporary = self.output_dir / ".session.json.tmp"
        destination = self.output_dir / "session.json"
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            payload = summary.model_dump(exclude={"segments"})
            temporary.write_text(
                json.dumps(
                    {
                        **payload,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(destination)
        except OSError:
            temporary.unlink(missing_ok=True)
            logger.exception(
                "Failed to write session summary for session %s.", self.session_id
            )
            if required:
                raise

    def _write_closed_session_marker(self, summary: LiveSessionEndResponse) -> None:
        """Persist an empty active session without exposing it as collected data."""

        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / CLOSED_SESSION_MARKER_FILENAME
        temporary = self.output_dir / f"{CLOSED_SESSION_MARKER_FILENAME}.tmp"
        try:
            temporary.write_text(
                json.dumps(summary.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(destination)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def _observed_events(
        self,
        chunks: list[_ChunkEntry],
        *,
        consumed_sequence_ids: tuple[int, ...],
        clip_end_sec: float | None,
    ) -> list[ObservedEvent]:
        consumed = set(consumed_sequence_ids)
        observations: list[ObservedEvent] = []
        for chunk in chunks:
            if chunk.sequence_id not in consumed:
                continue
            for event in chunk.events:
                # normalize_sound_events already offsets live events onto the
                # session timeline before they reach the collector.
                start_sec = event.start_time_sec
                end_sec = event.end_time_sec
                if clip_end_sec is not None:
                    end_sec = min(end_sec, clip_end_sec)
                if end_sec <= start_sec:
                    continue
                observations.append(
                    ObservedEvent(
                        source_sequence_id=chunk.sequence_id,
                        start_sec=start_sec,
                        end_sec=end_sec,
                        label=event.label,
                        confidence=event.confidence,
                    )
                )
        return observations

    def _decision_record(
        self,
        candidate: CandidateFeatures,
        decision: CurationDecision,
        *,
        reason: str | None = None,
    ) -> dict:
        summary = self._curator.summary()
        return {
            "candidate_id": candidate.candidate_id,
            "start_sec": round(candidate.start_sec, 3),
            "end_sec": round(candidate.end_sec, 3),
            "signature": list(candidate.signature),
            "primary_label": candidate.primary_label,
            "quota_label": decision.quota_label,
            "selected": False,
            "reason": reason or decision.reason,
            "policy_version": decision.policy_version,
            "budget": {
                "selected_segments": summary.policy_selected_segment_count,
                "selected_duration_sec": summary.policy_selected_duration_sec,
                "selected_audio_bytes": summary.policy_selected_audio_bytes,
            },
        }

    def _append_decision_record(self, record: dict) -> None:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            with (self.output_dir / "decisions.jsonl").open(
                "a", encoding="utf-8"
            ) as destination:
                destination.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception(
                "Failed to append curation decision for session %s.", self.session_id
            )

    def _delete_chunks(self, chunks: Sequence[_ChunkEntry]) -> None:
        for chunk in chunks:
            self._delete_chunk_file(chunk)

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


@dataclass(frozen=True)
class _MergeResult:
    audio: _MergedAudio | None
    consumed_sequence_ids: tuple[int, ...]
    skipped_sequence_ids: tuple[int, ...]


def _merge_chunk_audio(
    chunks: list[_ChunkEntry],
    *,
    clip_end_sec: float | None = None,
) -> _MergeResult:
    """Concatenate overlapping WAV windows into one continuous PCM stream.

    Windows overlap (2 s window, 1 s hop), so each chunk after the first
    only contributes the frames past the previous max window end. Chunks
    The result reports every skipped source so the caller can fail closed.
    """
    merged: bytearray | None = None
    params: tuple[int, int, int] | None = None
    prev_end_sec = 0.0
    consumed_sequence_ids: list[int] = []
    skipped_sequence_ids: list[int] = []

    for chunk in chunks:
        effective_end_sec = min(
            chunk.window_end_sec,
            clip_end_sec if clip_end_sec is not None else float("inf"),
        )
        if effective_end_sec <= chunk.window_start_sec + _TIME_EPSILON_SEC:
            continue
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
            skipped_sequence_ids.append(chunk.sequence_id)
            continue

        nchannels, sampwidth, framerate = chunk_params
        bytes_per_frame = nchannels * sampwidth
        effective_frame_count = max(
            0,
            round((effective_end_sec - chunk.window_start_sec) * framerate),
        )
        frames = frames[: effective_frame_count * bytes_per_frame]
        if not frames:
            skipped_sequence_ids.append(chunk.sequence_id)
            continue

        if merged is None:
            merged = bytearray(frames)
            params = chunk_params
            prev_end_sec = effective_end_sec
            consumed_sequence_ids.append(chunk.sequence_id)
            continue

        if chunk_params != params:
            logger.warning(
                "Skipping live chunk %s with mismatched WAV params %s (expected %s).",
                chunk.wav_path,
                chunk_params,
                params,
            )
            skipped_sequence_ids.append(chunk.sequence_id)
            continue

        nchannels, sampwidth, framerate = params
        bytes_per_frame = nchannels * sampwidth
        overlap_sec = prev_end_sec - chunk.window_start_sec
        skip_bytes = max(0, round(overlap_sec * framerate)) * bytes_per_frame
        if skip_bytes < len(frames):
            merged.extend(frames[skip_bytes:])
        prev_end_sec = max(prev_end_sec, effective_end_sec)
        consumed_sequence_ids.append(chunk.sequence_id)

    if merged is None or params is None:
        return _MergeResult(
            audio=None,
            consumed_sequence_ids=tuple(consumed_sequence_ids),
            skipped_sequence_ids=tuple(skipped_sequence_ids),
        )
    nchannels, sampwidth, framerate = params
    return _MergeResult(
        audio=_MergedAudio(
            nchannels=nchannels,
            sampwidth=sampwidth,
            framerate=framerate,
            frames=bytes(merged),
        ),
        consumed_sequence_ids=tuple(consumed_sequence_ids),
        skipped_sequence_ids=tuple(skipped_sequence_ids),
    )


def _write_wav(path: Path, audio: _MergedAudio) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(audio.nchannels)
        writer.setsampwidth(audio.sampwidth)
        writer.setframerate(audio.framerate)
        writer.writeframes(audio.frames)


class LiveCollectionManager:
    """Tracks one SegmentCollector per live session across requests.

    Ended sessions leave a tombstone so chunks whose analyses complete after
    `end_session` are deleted immediately instead of respawning a collector
    that would strand files in `recordings/live/`.
    """

    def __init__(
        self,
        stale_session_sec: float = DEFAULT_STALE_SESSION_SEC,
        collected_root: Path | None = None,
    ):
        self.stale_session_sec = stale_session_sec
        self.collected_root = collected_root
        self._lock = Lock()
        self._collectors: dict[str, SegmentCollector] = {}
        self._ended_sessions: dict[str, float] = {}
        self._ended_summaries: dict[str, LiveSessionEndResponse] = {}
        self._ending_sessions: dict[str, Event] = {}
        self._accepting_chunks = True

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
            if not self._accepting_chunks or session_id in self._ended_sessions:
                _discard_late_chunk(wav_path)
                return CHUNK_DISCARDED_LATE
            collector = self._collectors.get(session_id)
            if collector is None:
                persisted_summary = _load_live_session_end_response(output_dir)
                if persisted_summary is not None or _has_persisted_session_state(
                    output_dir
                ):
                    # A restarted process (or an expired in-memory tombstone)
                    # must never reuse a durable session id. Starting at
                    # segment index 1 in an existing directory could overwrite
                    # audio and replace its aggregate summary.
                    self._ended_sessions[session_id] = monotonic()
                    if (
                        persisted_summary is not None
                        and persisted_summary.ended_at is not None
                    ):
                        self._ended_summaries[session_id] = persisted_summary
                    _discard_late_chunk(wav_path)
                    return CHUNK_DISCARDED_LATE
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
            stale_session_ids = self._stale_session_ids(exclude=session_id)
        for stale_session_id in stale_session_ids:
            self.end_session(stale_session_id)
        return collector.add_chunk(
            sequence_id=sequence_id,
            window_start_sec=window_start_sec,
            window_end_sec=window_end_sec,
            wav_path=wav_path,
            events=events,
        )

    def get_curation_progress(
        self,
        session_id: str,
    ) -> LiveCurationProgress | None:
        with self._lock:
            collector = self._collectors.get(session_id)
            ended_summary = self._ended_summaries.get(session_id)
        if collector is not None:
            return collector.curation_progress()
        if ended_summary is None:
            return None
        return LiveCurationProgress(
            candidate_segment_count=ended_summary.candidate_segment_count,
            selected_segment_count=ended_summary.segment_count,
            rejected_repetitive_count=ended_summary.rejected_repetitive_count,
            rejected_class_balance_count=(
                ended_summary.rejected_class_balance_count
            ),
            rejected_session_budget_count=(
                ended_summary.rejected_session_budget_count
            ),
            invalid_audio_count=ended_summary.invalid_audio_count,
            write_error_count=ended_summary.write_error_count,
        )

    def end_session(
        self,
        session_id: str,
        session_name: str | None = None,
        output_dir: Path | None = None,
    ) -> LiveSessionEndResponse:
        while True:
            with self._lock:
                self._prune_tombstones()
                cached = self._ended_summaries.get(session_id)
                if cached is not None:
                    return cached
                ending = self._ending_sessions.get(session_id)
                if ending is None:
                    ending = Event()
                    self._ending_sessions[session_id] = ending
                    self._ended_sessions[session_id] = monotonic()
                    collector = self._collectors.pop(session_id, None)
                    break
            # A concurrent caller owns finalization. Waiting here ensures both
            # callers observe the same persisted/cached response.
            ending.wait()

        summary: LiveSessionEndResponse | None = None
        try:
            if collector is not None:
                if collector.session_name is None and session_name:
                    collector.session_name = session_name
                summary = collector.end_session()
            else:
                session_dir = self._session_dir(session_id, output_dir)
                if session_dir is not None:
                    summary = _load_live_session_end_response(session_dir)
                    if summary is not None and summary.ended_at is None:
                        summary = _finalize_recovered_session(
                            session_dir,
                            summary,
                            session_name=session_name,
                        )
                if summary is None:
                    summary = _empty_session_summary(session_id, session_name)
            return summary
        finally:
            with self._lock:
                if summary is not None:
                    self._ended_summaries[session_id] = summary
                elif collector is not None:
                    # Finalization can fail at its durable commit point. Keep
                    # the collector available so this or a concurrent caller
                    # can retry instead of receiving a false empty summary.
                    self._collectors.setdefault(session_id, collector)
                completed = self._ending_sessions.pop(session_id, None)
                if completed is not None:
                    completed.set()

    def end_all_sessions(self) -> list[LiveSessionEndResponse]:
        """Stop accepting chunks and idempotently finalize every live collector."""
        with self._lock:
            self._accepting_chunks = False
            session_ids = sorted(
                set(self._collectors).union(self._ending_sessions)
            )
        summaries: list[LiveSessionEndResponse] = []
        first_error: Exception | None = None
        for session_id in session_ids:
            try:
                summaries.append(self.end_session(session_id))
            except Exception as exc:
                # A broken session must not prevent every other session from
                # reaching its own durable terminal state during shutdown.
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
        return summaries

    def recover_incomplete_sessions(
        self,
        collected_root: Path | None = None,
    ) -> list[LiveSessionEndResponse]:
        """Close persisted sessions left open by a previous process.

        Segment files and aggregate counters are rebuilt from disk. The method
        does not guess at orphaned raw live chunks; callers may clean that
        staging area separately after this durable summary recovery succeeds.
        """
        root = collected_root or self.collected_root
        if root is None or not root.is_dir():
            return []

        recovered: list[LiveSessionEndResponse] = []
        for session_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            summary = _load_live_session_end_response(session_dir)
            if summary is None:
                continue
            if summary.ended_at is None:
                summary = _finalize_recovered_session(session_dir, summary)
                recovered.append(summary)
            with self._lock:
                self._ended_sessions[summary.session_id] = monotonic()
                self._ended_summaries[summary.session_id] = summary
        return recovered

    def _session_dir(
        self,
        session_id: str,
        output_dir: Path | None,
    ) -> Path | None:
        if output_dir is not None:
            return output_dir if output_dir.is_dir() else None
        if self.collected_root is None:
            return None
        return safe_collected_session_dir(self.collected_root, session_id)

    def _stale_session_ids(
        self,
        exclude: str,
    ) -> list[str]:
        now = monotonic()
        return [
            session_id
            for session_id, collector in self._collectors.items()
            if session_id != exclude
            and now - collector.last_activity_monotonic > self.stale_session_sec
        ]

    def _prune_tombstones(self) -> None:
        now = monotonic()
        expired = [
            session_id
            for session_id, ended_at in self._ended_sessions.items()
            if now - ended_at > self.stale_session_sec
        ]
        for session_id in expired:
            del self._ended_sessions[session_id]
            self._ended_summaries.pop(session_id, None)


def _discard_late_chunk(wav_path: Path) -> None:
    try:
        wav_path.unlink(missing_ok=True)
        wav_path.parent.rmdir()
    except OSError:
        # Parent not empty or already gone — nothing else to clean.
        pass


def _has_persisted_session_state(output_dir: Path) -> bool:
    if not output_dir.is_dir():
        return False
    # A decisions log or interrupted atomic-write temp is still evidence that
    # this id has owned durable state. Fail closed instead of appending a new
    # segment index sequence into any non-empty recovered directory.
    return any(output_dir.iterdir())


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
    session_payload: dict = {}
    session_json = session_dir / "session.json"
    if session_json.is_file():
        try:
            loaded = json.loads(session_json.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                session_payload = loaded
            else:
                logger.warning("Ignoring non-object session summary %s.", session_json)
        except (OSError, json.JSONDecodeError):
            logger.warning("Skipping unreadable session summary %s.", session_json)

    segments: list[CollectedSegmentSummary] = []
    for metadata_path in sorted_segment_metadata_paths(session_dir):
        try:
            segment = _load_collected_segment(
                session_dir,
                metadata_path,
                fallback_index=len(segments) + 1,
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping malformed segment metadata %s: %s",
                metadata_path,
                exc,
            )
            continue
        if segment is not None:
            segments.append(segment)

    if not segments and not session_payload:
        return None

    selected_label_segment_counts: dict[str, int] = {}
    selected_quota_duration_sec: dict[str, float] = {}
    for segment in segments:
        for label in segment.labels:
            selected_label_segment_counts[label] = (
                selected_label_segment_counts.get(label, 0) + 1
            )
        if segment.quota_label:
            selected_quota_duration_sec[segment.quota_label] = round(
                selected_quota_duration_sec.get(segment.quota_label, 0.0)
                + segment.duration_sec,
                3,
            )

    gcs_upload = _load_gcs_upload_status(session_dir)
    return CollectedSessionInfo(
        session_id=session_dir.name,
        session_name=_optional_text(session_payload.get("session_name")),
        started_at=_optional_text(session_payload.get("started_at")),
        ended_at=_optional_text(session_payload.get("ended_at")),
        segment_count=len(segments),
        total_collected_duration_sec=round(
            sum(segment.duration_sec for segment in segments), 3
        ),
        segments=segments,
        gcs_upload=gcs_upload,
        candidate_segment_count=_payload_nonnegative_int(
            session_payload, "candidate_segment_count"
        ),
        policy_selected_segment_count=_payload_nonnegative_int(
            session_payload, "policy_selected_segment_count"
        ),
        policy_selected_duration_sec=_payload_nonnegative_float(
            session_payload, "policy_selected_duration_sec"
        ),
        policy_selected_audio_bytes=_payload_nonnegative_int(
            session_payload, "policy_selected_audio_bytes"
        ),
        rejected_repetitive_count=_payload_nonnegative_int(
            session_payload, "rejected_repetitive_count"
        ),
        rejected_class_balance_count=_payload_nonnegative_int(
            session_payload, "rejected_class_balance_count"
        ),
        rejected_session_budget_count=_payload_nonnegative_int(
            session_payload, "rejected_session_budget_count"
        ),
        invalid_audio_count=_payload_nonnegative_int(
            session_payload, "invalid_audio_count"
        ),
        write_error_count=_payload_nonnegative_int(
            session_payload, "write_error_count"
        ),
        selected_label_segment_counts=selected_label_segment_counts,
        selected_quota_duration_sec=selected_quota_duration_sec,
        policy_version=_payload_optional_positive_int(
            session_payload, "policy_version"
        ),
    )


def _load_collected_segment(
    session_dir: Path,
    metadata_path: Path,
    *,
    fallback_index: int,
) -> CollectedSegmentSummary | None:
    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("metadata must be a JSON object")
    audio_path = resolve_segment_audio(session_dir, metadata_path.stem)
    if audio_path is None:
        return None

    events = data.get("events") or []
    if not isinstance(events, list) or any(
        not isinstance(event, dict) for event in events
    ):
        raise ValueError("events must be a list of JSON objects")
    curation = data.get("curation") or {}
    if not isinstance(curation, dict):
        raise ValueError("curation must be a JSON object")

    labels: set[str] = set()
    for event in events:
        raw_label = event.get("label")
        if raw_label is None or raw_label == "":
            continue
        if not isinstance(raw_label, str):
            raise ValueError("event labels must be strings")
        labels.add(raw_label)

    segment_index = _positive_int(data.get("segment_index"), fallback_index)
    start_sec = _finite_float(data.get("start_sec"), 0.0)
    end_sec = _finite_float(data.get("end_sec"), 0.0)
    duration_sec = _finite_float(data.get("duration_sec"), 0.0)
    if start_sec < 0 or end_sec < start_sec or duration_sec < 0:
        raise ValueError("segment times must be non-negative and ordered")

    return CollectedSegmentSummary(
        segment_index=segment_index,
        start_sec=start_sec,
        end_sec=end_sec,
        duration_sec=duration_sec,
        event_count=len(events),
        labels=sorted(labels),
        audio_filename=audio_path.name,
        metadata_filename=metadata_path.name,
        primary_label=_optional_text(curation.get("primary_label")),
        quota_label=_optional_text(curation.get("quota_label")),
        selection_reason=(
            _optional_text(curation.get("selection_reason")) or "legacy"
        ),
    )


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _finite_float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric value")
    converted = float(value)
    if not isfinite(converted):
        raise ValueError("numeric value must be finite")
    return converted


def _positive_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer value")
    converted = int(value)
    if converted <= 0:
        raise ValueError("integer value must be positive")
    return converted


def _payload_nonnegative_int(payload: dict, key: str) -> int:
    try:
        value = payload.get(key)
        if value is None:
            return 0
        if isinstance(value, bool):
            raise ValueError
        converted = int(value)
        if converted < 0:
            raise ValueError
        return converted
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s in session summary.", key)
        return 0


def _payload_nonnegative_float(payload: dict, key: str) -> float:
    try:
        converted = _finite_float(payload.get(key), 0.0)
        if converted < 0:
            raise ValueError
        return converted
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s in session summary.", key)
        return 0.0


def _payload_optional_positive_int(payload: dict, key: str) -> int | None:
    if payload.get(key) is None:
        return None
    try:
        return _positive_int(payload[key], 1)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s in session summary.", key)
        return None


def _load_live_session_end_response(
    session_dir: Path,
) -> LiveSessionEndResponse | None:
    session_json = session_dir / "session.json"
    if not session_json.is_file():
        closed_marker = session_dir / CLOSED_SESSION_MARKER_FILENAME
        if closed_marker.is_file():
            try:
                payload = json.loads(closed_marker.read_text(encoding="utf-8"))
                summary = LiveSessionEndResponse.model_validate(payload)
            except (OSError, json.JSONDecodeError, ValueError):
                logger.warning("Cannot recover empty-session marker %s.", closed_marker)
            else:
                if summary.session_id == session_dir.name and summary.ended_at is not None:
                    return summary
                logger.warning("Ignoring invalid empty-session marker %s.", closed_marker)
        return _synthesize_session_summary_from_segments(session_dir)
    try:
        payload = json.loads(session_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Cannot recover unreadable session summary %s.", session_json)
        return None
    if not isinstance(payload, dict):
        logger.warning("Cannot recover non-object session summary %s.", session_json)
        return None

    info = _load_collected_session(session_dir)
    if info is None:
        return None
    return LiveSessionEndResponse(
        session_id=info.session_id,
        session_name=info.session_name,
        started_at=info.started_at,
        ended_at=info.ended_at,
        segment_count=info.segment_count,
        total_collected_duration_sec=info.total_collected_duration_sec,
        kept_chunk_count=_payload_nonnegative_int(payload, "kept_chunk_count"),
        discarded_silent_chunk_count=_payload_nonnegative_int(
            payload, "discarded_silent_chunk_count"
        ),
        discarded_speech_chunk_count=_payload_nonnegative_int(
            payload, "discarded_speech_chunk_count"
        ),
        segments=info.segments,
        candidate_segment_count=info.candidate_segment_count,
        policy_selected_segment_count=info.policy_selected_segment_count,
        policy_selected_duration_sec=info.policy_selected_duration_sec,
        policy_selected_audio_bytes=info.policy_selected_audio_bytes,
        rejected_repetitive_count=info.rejected_repetitive_count,
        rejected_class_balance_count=info.rejected_class_balance_count,
        rejected_session_budget_count=info.rejected_session_budget_count,
        invalid_audio_count=info.invalid_audio_count,
        write_error_count=info.write_error_count,
        selected_label_segment_counts=info.selected_label_segment_counts,
        selected_quota_duration_sec=info.selected_quota_duration_sec,
        policy_version=info.policy_version,
    )


def _synthesize_session_summary_from_segments(
    session_dir: Path,
) -> LiveSessionEndResponse | None:
    """Rebuild the minimum open summary after a crash before session.json publish."""

    info = _load_collected_session(session_dir)
    if info is None or not info.segments:
        return None

    session_name: str | None = None
    started_at: str | None = None
    policy_version: int | None = info.policy_version
    chunk_sequence_ids: set[int] = set()
    for metadata_path in sorted_segment_metadata_paths(session_dir):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if session_name is None:
            session_name = _optional_text(payload.get("session_name"))
        if started_at is None:
            started_at = _optional_text(payload.get("session_started_at"))
        raw_sequence_ids = payload.get("chunk_sequence_ids")
        if isinstance(raw_sequence_ids, list):
            for value in raw_sequence_ids:
                if isinstance(value, bool):
                    continue
                try:
                    sequence_id = int(value)
                except (TypeError, ValueError):
                    continue
                if sequence_id > 0:
                    chunk_sequence_ids.add(sequence_id)
        curation = payload.get("curation")
        if policy_version is None and isinstance(curation, dict):
            raw_policy_version = curation.get("policy_version")
            if raw_policy_version is not None:
                try:
                    policy_version = _positive_int(raw_policy_version, 1)
                except (TypeError, ValueError):
                    pass

    audio_bytes = 0
    for segment in info.segments:
        audio_path = session_dir / segment.audio_filename
        try:
            audio_bytes += audio_path.stat().st_size
        except OSError:
            # The segment loader already required audio. A concurrent loss is
            # reflected by the startup cleanup/listing pass rather than guessed.
            return None

    return LiveSessionEndResponse(
        session_id=session_dir.name,
        session_name=session_name,
        started_at=started_at,
        ended_at=None,
        segment_count=len(info.segments),
        total_collected_duration_sec=info.total_collected_duration_sec,
        kept_chunk_count=len(chunk_sequence_ids) or len(info.segments),
        discarded_silent_chunk_count=0,
        discarded_speech_chunk_count=0,
        segments=info.segments,
        candidate_segment_count=len(info.segments),
        policy_selected_segment_count=len(info.segments),
        policy_selected_duration_sec=info.total_collected_duration_sec,
        policy_selected_audio_bytes=audio_bytes,
        selected_label_segment_counts=info.selected_label_segment_counts,
        selected_quota_duration_sec=info.selected_quota_duration_sec,
        policy_version=policy_version,
    )


def _finalize_recovered_session(
    session_dir: Path,
    summary: LiveSessionEndResponse,
    *,
    session_name: str | None = None,
) -> LiveSessionEndResponse:
    if summary.ended_at is not None:
        return summary
    finalized = summary.model_copy(
        update={
            "session_name": summary.session_name or session_name,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    session_json = session_dir / "session.json"
    if session_json.is_file():
        payload = json.loads(session_json.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Persisted session summary must be a JSON object.")
    else:
        payload = {"recovered_from_segment_metadata": True}
    payload.update(finalized.model_dump(exclude={"segments"}))
    payload["recovered_at"] = datetime.now(timezone.utc).isoformat()
    temporary = session_dir / ".session.recovery.json.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(session_json)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return finalized


def _empty_session_summary(
    session_id: str,
    session_name: str | None,
) -> LiveSessionEndResponse:
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


def _load_gcs_upload_status(session_dir: Path) -> GcsUploadStatus | None:
    marker = session_dir / UPLOAD_MARKER_FILENAME
    if not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        return GcsUploadStatus.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError):
        logger.warning("Ignoring unreadable GCS upload marker %s.", marker)
        return None


def delete_collected_session(collected_root: Path, session_id: str) -> bool:
    session_dir = safe_collected_session_dir(collected_root, session_id)
    if session_dir is None:
        return False
    with _segment_file_lock:
        # Re-resolve under the publish/delete lock so a concurrent conversion
        # cannot recreate a file after this directory is removed.
        session_dir = safe_collected_session_dir(collected_root, session_id)
        if session_dir is None:
            return False
        try:
            shutil.rmtree(session_dir)
        except FileNotFoundError:
            return False
    return True


def publish_segment_conversion(wav_path: Path, temporary_mp3_path: Path) -> bool:
    """Atomically publish a conversion only while its segment still exists.

    The conversion itself may run without the lock. Callers must use this
    helper for the final rename; it serializes that small commit with segment
    and session deletion, and checks both semantic metadata and a deletion
    tombstone before making the MP3 visible.
    """
    stem = wav_path.stem
    session_dir = wav_path.parent
    metadata_path = session_dir / f"{stem}.json"
    tombstone = _segment_delete_tombstone(session_dir, stem)
    destination = session_dir / f"{stem}.mp3"
    with _segment_file_lock:
        if tombstone.exists() or not metadata_path.is_file():
            return False
        temporary_mp3_path.replace(destination)
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
    with _segment_file_lock:
        session_dir = safe_collected_session_dir(collected_root, session_id)
        if session_dir is None:
            return False
        targets = [
            session_dir / f"{stem}{suffix}"
            for suffix in (".wav", ".mp3", ".json")
        ]
        existing_targets = [target for target in targets if target.is_file()]
        if not existing_targets:
            return False

        # The tombstone is committed before any source disappears. A conversion
        # that finishes later must consult publish_segment_conversion and will
        # therefore fail closed.
        _segment_delete_tombstone(session_dir, stem).touch(exist_ok=True)
        _invalidate_gcs_upload_marker(session_dir, deleted_segment=filename)
        for target in existing_targets:
            target.unlink()

        session_json = session_dir / "session.json"
        if not session_json.is_file():
            if not any(session_dir.glob("segment-*.json")):
                shutil.rmtree(session_dir)
            return True

        session = _load_collected_session(session_dir)
        if session is None:
            raise ValueError(
                f"Could not rebuild session summary after deleting {filename}."
            )
        payload = json.loads(session_json.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Session summary must be a JSON object.")
        payload.update(
            {
                "segment_count": session.segment_count,
                "total_collected_duration_sec": (
                    session.total_collected_duration_sec
                ),
                "selected_label_segment_counts": (
                    session.selected_label_segment_counts
                ),
                "selected_quota_duration_sec": (
                    session.selected_quota_duration_sec
                ),
            }
        )
        temporary = session_dir / ".session.json.tmp"
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(session_json)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
    return True


def _segment_delete_tombstone(session_dir: Path, stem: str) -> Path:
    return session_dir / f".{stem}{_SEGMENT_DELETE_TOMBSTONE_SUFFIX}"


def _invalidate_gcs_upload_marker(
    session_dir: Path,
    *,
    deleted_segment: str,
) -> None:
    marker = session_dir / UPLOAD_MARKER_FILENAME
    if not marker.is_file():
        return
    stale_marker = session_dir / STALE_UPLOAD_MARKER_FILENAME
    marker.replace(stale_marker)

    # The rename above is the atomic invalidation. Enrichment is best-effort:
    # even a malformed legacy marker remains inactive under the stale name.
    temporary = session_dir / f"{STALE_UPLOAD_MARKER_FILENAME}.tmp"
    try:
        payload = json.loads(stale_marker.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        payload.update(
            {
                "status": "stale",
                "invalidated_at": datetime.now(timezone.utc).isoformat(),
                "invalidated_reason": "local_segment_deleted",
                "invalidated_segment_filename": deleted_segment,
            }
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(stale_marker)
    except (OSError, json.JSONDecodeError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning("Could not enrich stale GCS marker %s.", stale_marker)
