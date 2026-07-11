from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import isfinite
from typing import Literal, Sequence

CurationReason = Literal[
    "new_label",
    "balanced",
    "repetitive",
    "class_balance",
    "session_budget",
    "invalid_audio",
    "write_error",
]

BALANCE_MIN_SELECTED_SEGMENTS = 10
_TIME_EPSILON_SEC = 1e-9


def normalize_label(label: str) -> str:
    return label.strip().casefold()


@dataclass(frozen=True)
class CurationPolicy:
    max_segments: int = 600
    max_duration_sec: float = 3600.0
    max_audio_bytes: int = 512 * 1024 * 1024
    repeat_cooldown_sec: float = 600.0
    max_quota_label_share: float = 0.30
    policy_version: int = 1

    def __post_init__(self) -> None:
        numeric = (
            self.max_segments,
            self.max_duration_sec,
            self.max_audio_bytes,
            self.repeat_cooldown_sec,
            self.max_quota_label_share,
        )
        if not all(isfinite(value) for value in numeric):
            raise ValueError("Curation numeric settings must be finite.")
        if self.max_segments <= 0:
            raise ValueError("Curation max segments must be positive.")
        if self.max_duration_sec <= 0:
            raise ValueError("Curation max duration must be positive.")
        if self.max_audio_bytes <= 0:
            raise ValueError("Curation max audio bytes must be positive.")
        if self.repeat_cooldown_sec < 0:
            raise ValueError("Curation repeat cooldown cannot be negative.")
        if not 0 < self.max_quota_label_share <= 1:
            raise ValueError("Curation label share must be greater than 0 and at most 1.")
        if self.policy_version <= 0:
            raise ValueError("Curation policy version must be positive.")


@dataclass(frozen=True)
class EventTrack:
    label: str
    normalized_label: str
    start_sec: float
    end_sec: float
    max_confidence: float | None
    supporting_window_count: int


@dataclass(frozen=True)
class ObservedEvent:
    source_sequence_id: int
    start_sec: float
    end_sec: float
    label: str
    confidence: float | None


@dataclass(frozen=True)
class CandidateFeatures:
    candidate_id: int
    start_sec: float
    end_sec: float
    duration_sec: float
    estimated_audio_bytes: int
    tracks: tuple[EventTrack, ...]
    signature: tuple[str, ...]
    primary_label: str


@dataclass(frozen=True)
class CurationDecision:
    selected: bool
    reason: CurationReason
    quota_label: str
    policy_version: int


@dataclass(frozen=True)
class CurationSummary:
    candidate_segment_count: int
    policy_selected_segment_count: int
    policy_selected_duration_sec: float
    policy_selected_audio_bytes: int
    rejected_repetitive_count: int
    rejected_class_balance_count: int
    rejected_session_budget_count: int
    invalid_audio_count: int
    write_error_count: int
    selected_label_segment_counts: dict[str, int]
    selected_quota_duration_sec: dict[str, float]
    policy_version: int


def consolidate_events(
    observations: Sequence[ObservedEvent],
    confidence_threshold: float,
    merge_gap_sec: float = 0.75,
) -> tuple[EventTrack, ...]:
    """Merge overlapping observations into stable per-label event tracks."""
    if not 0 <= confidence_threshold <= 1:
        raise ValueError("Confidence threshold must be between 0 and 1.")
    if merge_gap_sec < 0:
        raise ValueError("Merge gap cannot be negative.")

    grouped: dict[str, list[ObservedEvent]] = defaultdict(list)
    for observation in observations:
        normalized = normalize_label(observation.label)
        if not normalized or observation.end_sec <= observation.start_sec:
            continue
        if (
            observation.confidence is not None
            and observation.confidence < confidence_threshold
        ):
            continue
        grouped[normalized].append(observation)

    tracks: list[EventTrack] = []
    for normalized, label_observations in grouped.items():
        ordered = sorted(
            label_observations,
            key=lambda item: (item.start_sec, item.end_sec, item.source_sequence_id),
        )
        current: list[ObservedEvent] = []
        current_end = 0.0
        for observation in ordered:
            if current and observation.start_sec > current_end + merge_gap_sec:
                tracks.append(_make_track(normalized, current))
                current = []
            current.append(observation)
            current_end = max(current_end, observation.end_sec)
        if current:
            tracks.append(_make_track(normalized, current))

    return tuple(
        sorted(tracks, key=lambda item: (item.start_sec, item.normalized_label, item.end_sec))
    )


def build_candidate_features(
    *,
    candidate_id: int,
    start_sec: float,
    end_sec: float,
    duration_sec: float,
    estimated_audio_bytes: int,
    observations: Sequence[ObservedEvent],
    confidence_threshold: float,
) -> CandidateFeatures:
    tracks = consolidate_events(observations, confidence_threshold)
    if not tracks:
        raise ValueError("A curation candidate must contain at least one event track.")
    signature = tuple(sorted({track.normalized_label for track in tracks}))
    duration_by_label: dict[str, float] = defaultdict(float)
    confidence_by_label: dict[str, float] = {}
    for track in tracks:
        duration_by_label[track.normalized_label] += track.end_sec - track.start_sec
        if track.max_confidence is not None:
            confidence_by_label[track.normalized_label] = max(
                confidence_by_label.get(track.normalized_label, float("-inf")),
                track.max_confidence,
            )
    primary_label = min(
        signature,
        key=lambda label: (
            -duration_by_label[label],
            -confidence_by_label.get(label, float("-inf")),
            label,
        ),
    )
    return CandidateFeatures(
        candidate_id=candidate_id,
        start_sec=start_sec,
        end_sec=end_sec,
        duration_sec=duration_sec,
        estimated_audio_bytes=estimated_audio_bytes,
        tracks=tracks,
        signature=signature,
        primary_label=primary_label,
    )


def _make_track(normalized_label: str, observations: Sequence[ObservedEvent]) -> EventTrack:
    confidences = [
        observation.confidence
        for observation in observations
        if observation.confidence is not None
    ]
    representative = min(
        observations,
        key=lambda item: (
            -(item.confidence if item.confidence is not None else float("-inf")),
            item.label.casefold(),
        ),
    )
    return EventTrack(
        label=representative.label.strip(),
        normalized_label=normalized_label,
        start_sec=min(item.start_sec for item in observations),
        end_sec=max(item.end_sec for item in observations),
        max_confidence=max(confidences) if confidences else None,
        supporting_window_count=len(
            {item.source_sequence_id for item in observations}
        ),
    )


class SegmentCurator:
    """Own all session-level selection state and hide policy sequencing."""

    def __init__(self, policy: CurationPolicy):
        self.policy = policy
        self._candidate_segment_count = 0
        self._selected_segment_count = 0
        self._selected_duration_sec = 0.0
        self._selected_audio_bytes = 0
        self._rejected_repetitive_count = 0
        self._rejected_class_balance_count = 0
        self._rejected_session_budget_count = 0
        self._invalid_audio_count = 0
        self._write_error_count = 0
        self._selected_label_segment_counts: dict[str, int] = defaultdict(int)
        self._selected_quota_duration_sec: dict[str, float] = defaultdict(float)
        self._last_selected_signature_end_sec: dict[tuple[str, ...], float] = {}
        self._recorded_candidate_ids: set[int] = set()

    def evaluate(self, candidate: CandidateFeatures) -> CurationDecision:
        quota_label = self._quota_label(candidate)
        if self._would_exceed_budget(candidate):
            return self._decision(False, "session_budget", quota_label)

        if any(
            self._selected_label_segment_counts.get(label, 0) == 0
            for label in candidate.signature
        ):
            return self._decision(True, "new_label", quota_label)

        last_end = self._last_selected_signature_end_sec.get(candidate.signature)
        if (
            last_end is not None
            and candidate.start_sec - last_end
            < self.policy.repeat_cooldown_sec - _TIME_EPSILON_SEC
        ):
            return self._decision(False, "repetitive", quota_label)

        if self._selected_segment_count >= BALANCE_MIN_SELECTED_SEGMENTS:
            projected_total = self._selected_duration_sec + candidate.duration_sec
            projected_quota = (
                self._selected_quota_duration_sec.get(quota_label, 0.0)
                + candidate.duration_sec
            )
            minimum_quota = min(
                self._selected_quota_duration_sec.get(label, 0.0)
                for label in self._selected_label_segment_counts
            )
            allowed_duration = max(
                self.policy.max_quota_label_share * projected_total,
                minimum_quota + candidate.duration_sec,
            )
            if projected_quota > allowed_duration + _TIME_EPSILON_SEC:
                return self._decision(False, "class_balance", quota_label)

        return self._decision(True, "balanced", quota_label)

    def commit_selected(
        self,
        candidate: CandidateFeatures,
        decision: CurationDecision,
    ) -> None:
        if not decision.selected:
            raise ValueError("Cannot commit a rejected curation decision.")
        self._record_candidate(candidate.candidate_id)
        self._selected_segment_count += 1
        self._selected_duration_sec += candidate.duration_sec
        self._selected_audio_bytes += candidate.estimated_audio_bytes
        for label in candidate.signature:
            self._selected_label_segment_counts[label] += 1
        self._selected_quota_duration_sec[decision.quota_label] += candidate.duration_sec
        self._last_selected_signature_end_sec[candidate.signature] = candidate.end_sec

    def record_rejected(
        self,
        candidate: CandidateFeatures,
        decision: CurationDecision,
    ) -> None:
        if decision.selected:
            raise ValueError("Cannot record a selected decision as rejected.")
        self._record_candidate(candidate.candidate_id)
        if decision.reason == "repetitive":
            self._rejected_repetitive_count += 1
        elif decision.reason == "class_balance":
            self._rejected_class_balance_count += 1
        elif decision.reason == "session_budget":
            self._rejected_session_budget_count += 1
        else:
            raise ValueError(f"Unsupported rejection reason: {decision.reason}")

    def record_invalid_audio(
        self,
        candidate_id: int,
        start_sec: float,
        end_sec: float,
        skipped_sequence_ids: tuple[int, ...],
    ) -> None:
        del start_sec, end_sec, skipped_sequence_ids
        self._record_candidate(candidate_id)
        self._invalid_audio_count += 1

    def record_write_error(self, candidate: CandidateFeatures) -> None:
        self._record_candidate(candidate.candidate_id)
        self._write_error_count += 1

    def summary(self) -> CurationSummary:
        return CurationSummary(
            candidate_segment_count=self._candidate_segment_count,
            policy_selected_segment_count=self._selected_segment_count,
            policy_selected_duration_sec=round(self._selected_duration_sec, 3),
            policy_selected_audio_bytes=self._selected_audio_bytes,
            rejected_repetitive_count=self._rejected_repetitive_count,
            rejected_class_balance_count=self._rejected_class_balance_count,
            rejected_session_budget_count=self._rejected_session_budget_count,
            invalid_audio_count=self._invalid_audio_count,
            write_error_count=self._write_error_count,
            selected_label_segment_counts=dict(
                sorted(self._selected_label_segment_counts.items())
            ),
            selected_quota_duration_sec={
                label: round(duration, 3)
                for label, duration in sorted(
                    self._selected_quota_duration_sec.items()
                )
            },
            policy_version=self.policy.policy_version,
        )

    def _quota_label(self, candidate: CandidateFeatures) -> str:
        minimum_duration = min(
            self._selected_quota_duration_sec.get(label, 0.0)
            for label in candidate.signature
        )
        tied = [
            label
            for label in candidate.signature
            if self._selected_quota_duration_sec.get(label, 0.0) == minimum_duration
        ]
        if candidate.primary_label in tied:
            return candidate.primary_label
        return min(tied)

    def _would_exceed_budget(self, candidate: CandidateFeatures) -> bool:
        return (
            self._selected_segment_count + 1 > self.policy.max_segments
            or self._selected_duration_sec + candidate.duration_sec
            > self.policy.max_duration_sec + _TIME_EPSILON_SEC
            or self._selected_audio_bytes + candidate.estimated_audio_bytes
            > self.policy.max_audio_bytes
        )

    def _record_candidate(self, candidate_id: int) -> None:
        if candidate_id in self._recorded_candidate_ids:
            raise ValueError(f"Candidate {candidate_id} already has a terminal outcome.")
        self._recorded_candidate_ids.add(candidate_id)
        self._candidate_segment_count += 1

    def _decision(
        self,
        selected: bool,
        reason: CurationReason,
        quota_label: str,
    ) -> CurationDecision:
        return CurationDecision(
            selected=selected,
            reason=reason,
            quota_label=quota_label,
            policy_version=self.policy.policy_version,
        )
