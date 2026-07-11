from __future__ import annotations

import pytest

from backend.app.curation import (
    BALANCE_MIN_SELECTED_SEGMENTS,
    CandidateFeatures,
    CurationPolicy,
    EventTrack,
    ObservedEvent,
    SegmentCurator,
    build_candidate_features,
    consolidate_events,
)


def candidate(
    candidate_id: int,
    label: str = "keyboard",
    *,
    start_sec: float | None = None,
    duration_sec: float = 20.0,
    labels: tuple[str, ...] | None = None,
    audio_bytes: int = 100,
) -> CandidateFeatures:
    start = start_sec if start_sec is not None else candidate_id * 1000.0
    signature = labels or (label,)
    tracks = tuple(
        EventTrack(
            label=item,
            normalized_label=item,
            start_sec=start,
            end_sec=start + duration_sec,
            max_confidence=0.9,
            supporting_window_count=2,
        )
        for item in signature
    )
    return CandidateFeatures(
        candidate_id=candidate_id,
        start_sec=start,
        end_sec=start + duration_sec,
        duration_sec=duration_sec,
        estimated_audio_bytes=audio_bytes,
        tracks=tracks,
        signature=tuple(sorted(signature)),
        primary_label=label,
    )


def commit(curator: SegmentCurator, item: CandidateFeatures):
    decision = curator.evaluate(item)
    assert decision.selected
    curator.commit_selected(item, decision)
    return decision


def reject(curator: SegmentCurator, item: CandidateFeatures):
    decision = curator.evaluate(item)
    assert not decision.selected
    curator.record_rejected(item, decision)
    return decision


def test_consolidate_events_counts_distinct_supporting_windows():
    tracks = consolidate_events(
        [
            ObservedEvent(1, 0.0, 1.0, " Keyboard ", 0.8),
            ObservedEvent(1, 0.2, 1.2, "keyboard", 0.9),
            ObservedEvent(2, 1.3, 2.0, "KEYBOARD", 0.7),
        ],
        confidence_threshold=0.5,
    )

    assert len(tracks) == 1
    assert tracks[0].normalized_label == "keyboard"
    assert tracks[0].start_sec == 0.0
    assert tracks[0].end_sec == 2.0
    assert tracks[0].max_confidence == 0.9
    assert tracks[0].supporting_window_count == 2


def test_consolidate_events_filters_low_confidence_and_splits_long_gaps():
    tracks = consolidate_events(
        [
            ObservedEvent(1, 0.0, 1.0, "alarm", 0.4),
            ObservedEvent(2, 0.0, 1.0, "alarm", None),
            ObservedEvent(3, 2.0, 3.0, "alarm", 0.8),
        ],
        confidence_threshold=0.5,
    )

    assert [(track.start_sec, track.end_sec) for track in tracks] == [
        (0.0, 1.0),
        (2.0, 3.0),
    ]


def test_build_candidate_chooses_primary_by_duration_then_confidence():
    features = build_candidate_features(
        candidate_id=1,
        start_sec=0.0,
        end_sec=5.0,
        duration_sec=5.0,
        estimated_audio_bytes=100,
        observations=[
            ObservedEvent(1, 0.0, 3.0, "fan", 0.6),
            ObservedEvent(1, 0.0, 2.0, "alarm", 0.99),
        ],
        confidence_threshold=0.5,
    )

    assert features.signature == ("alarm", "fan")
    assert features.primary_label == "fan"


def test_new_label_is_selected_but_never_bypasses_hard_budget():
    curator = SegmentCurator(
        CurationPolicy(max_segments=1, max_duration_sec=100, max_audio_bytes=1000)
    )

    assert commit(curator, candidate(1, "fan")).reason == "new_label"
    decision = reject(curator, candidate(2, "glass"))

    assert decision.reason == "session_budget"
    assert curator.summary().rejected_session_budget_count == 1


def test_evaluate_does_not_mutate_curator_state():
    curator = SegmentCurator(
        CurationPolicy(max_duration_sec=1.0, max_audio_bytes=1000)
    )
    before = curator.summary()

    decision = curator.evaluate(candidate(1, "unseen", duration_sec=2.0))

    assert decision.reason == "session_budget"
    assert curator.summary() == before


def test_same_signature_is_rejected_inside_cooldown():
    curator = SegmentCurator(CurationPolicy(repeat_cooldown_sec=600))
    commit(curator, candidate(1, start_sec=0.0))

    decision = reject(curator, candidate(2, start_sec=100.0))

    assert decision.reason == "repetitive"
    assert curator.summary().rejected_repetitive_count == 1


def test_two_label_balance_rejects_dominance_without_deadlock():
    curator = SegmentCurator(CurationPolicy(repeat_cooldown_sec=0))
    next_id = 1
    for _ in range(BALANCE_MIN_SELECTED_SEGMENTS // 2):
        commit(curator, candidate(next_id, "a"))
        next_id += 1
        commit(curator, candidate(next_id, "b"))
        next_id += 1

    accepted_after_balance = commit(curator, candidate(next_id, "a"))
    next_id += 1
    dominant = reject(curator, candidate(next_id, "a"))
    next_id += 1
    underrepresented = commit(curator, candidate(next_id, "b"))

    assert accepted_after_balance.reason == "balanced"
    assert dominant.reason == "class_balance"
    assert underrepresented.reason == "balanced"
    assert curator.summary().rejected_class_balance_count == 1


def test_balance_evaluation_is_pure_before_terminal_recording():
    curator = SegmentCurator(CurationPolicy(repeat_cooldown_sec=0))
    next_id = 1
    for _ in range(BALANCE_MIN_SELECTED_SEGMENTS // 2):
        commit(curator, candidate(next_id, "a"))
        next_id += 1
        commit(curator, candidate(next_id, "b"))
        next_id += 1
    commit(curator, candidate(next_id, "a"))
    next_id += 1
    before = curator.summary()

    decision = curator.evaluate(candidate(next_id, "a"))

    assert decision.reason == "class_balance"
    assert curator.summary() == before


def test_multilabel_candidate_is_charged_to_less_selected_label():
    curator = SegmentCurator(CurationPolicy(repeat_cooldown_sec=0))
    commit(curator, candidate(1, "fan"))
    commit(curator, candidate(2, "fan"))
    commit(curator, candidate(3, "glass"))

    decision = commit(
        curator,
        candidate(4, "fan", labels=("fan", "glass")),
    )

    assert decision.quota_label == "glass"


def test_single_label_long_run_remains_bounded_without_balance_deadlock():
    curator = SegmentCurator(
        CurationPolicy(
            max_segments=600,
            max_duration_sec=3600,
            max_audio_bytes=512 * 1024 * 1024,
            repeat_cooldown_sec=600,
        )
    )
    for candidate_id in range(1, 1896):
        item = candidate(
            candidate_id,
            start_sec=(candidate_id - 1) * 19.0,
            duration_sec=19.0,
            audio_bytes=19 * 96_000 + 44,
        )
        decision = curator.evaluate(item)
        if decision.selected:
            curator.commit_selected(item, decision)
        else:
            curator.record_rejected(item, decision)

    summary = curator.summary()
    assert summary.candidate_segment_count == 1895
    assert 10 < summary.policy_selected_segment_count <= 600
    assert summary.policy_selected_duration_sec <= 3600
    assert summary.policy_selected_audio_bytes <= 512 * 1024 * 1024
    assert summary.rejected_repetitive_count > 0


def test_late_new_label_is_selected_while_budget_remains():
    curator = SegmentCurator(CurationPolicy(repeat_cooldown_sec=600))
    for candidate_id in range(1, 50):
        item = candidate(candidate_id, "fan", start_sec=(candidate_id - 1) * 620.0)
        commit(curator, item)

    late = curator.evaluate(candidate(100, "glass", start_sec=9 * 3600.0))

    assert late.selected
    assert late.reason == "new_label"


def test_invalid_and_write_error_are_terminal_outcomes():
    curator = SegmentCurator(CurationPolicy())
    curator.record_invalid_audio(1, 0.0, 2.0, (1,))
    curator.record_write_error(candidate(2))

    summary = curator.summary()
    assert summary.candidate_segment_count == 2
    assert summary.invalid_audio_count == 1
    assert summary.write_error_count == 1
    with pytest.raises(ValueError, match="already has a terminal outcome"):
        curator.record_write_error(candidate(2))


@pytest.mark.parametrize(
    "policy",
    [
        CurationPolicy(max_segments=1),
        CurationPolicy(max_duration_sec=1),
        CurationPolicy(max_audio_bytes=1),
    ],
)
def test_each_hard_limit_is_enforced(policy: CurationPolicy):
    curator = SegmentCurator(policy)
    item = candidate(1, duration_sec=2.0, audio_bytes=2)
    decision = curator.evaluate(item)
    if decision.selected:
        curator.commit_selected(item, decision)
        decision = curator.evaluate(candidate(2, "new", duration_sec=2.0, audio_bytes=2))
    assert decision.reason == "session_budget"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_segments": 0},
        {"max_duration_sec": float("nan")},
        {"max_audio_bytes": 0},
        {"repeat_cooldown_sec": -1},
        {"max_quota_label_share": 0},
        {"max_quota_label_share": 1.1},
    ],
)
def test_policy_rejects_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        CurationPolicy(**kwargs)
