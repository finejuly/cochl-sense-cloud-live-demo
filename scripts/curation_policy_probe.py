#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.curation import (
    BALANCE_MIN_SELECTED_SEGMENTS,
    CandidateFeatures,
    CurationPolicy,
    EventTrack,
    SegmentCurator,
)


@dataclass(frozen=True)
class ProbeResult:
    scenario: str
    hours: float
    candidates: int
    selected_segments: int
    selected_duration_sec: float
    selected_audio_bytes: int
    rejected_repetitive: int
    rejected_class_balance: int
    rejected_session_budget: int
    accepted_after_balance_enabled: int
    late_label_selected: bool


def _candidate(
    candidate_id: int,
    start_sec: float,
    duration_sec: float,
    label: str,
) -> CandidateFeatures:
    return CandidateFeatures(
        candidate_id=candidate_id,
        start_sec=start_sec,
        end_sec=start_sec + duration_sec,
        duration_sec=duration_sec,
        estimated_audio_bytes=round(duration_sec * 96_000) + 44,
        tracks=(
            EventTrack(
                label=label,
                normalized_label=label,
                start_sec=start_sec,
                end_sec=start_sec + duration_sec,
                max_confidence=0.9,
                supporting_window_count=max(1, round(duration_sec)),
            ),
        ),
        signature=(label,),
        primary_label=label,
    )


def run_probe(scenario: str, hours: float) -> ProbeResult:
    total_sec = hours * 3600.0
    candidate_count = math.ceil(total_sec / 19.0)
    policy = CurationPolicy(repeat_cooldown_sec=0 if scenario == "imbalanced" else 600)
    curator = SegmentCurator(policy)
    accepted_after_balance = 0
    late_label_selected = False
    late_emitted = False

    for index in range(candidate_count):
        start_sec = index * 19.0
        duration_sec = min(19.0, total_sec - start_sec)
        if scenario == "imbalanced":
            label = "minority" if index % 4 == 3 else "dominant"
        elif scenario == "rare-late" and not late_emitted and start_sec >= 9 * 3600:
            label = "rare"
            late_emitted = True
        else:
            label = "dominant"
        item = _candidate(index + 1, start_sec, duration_sec, label)
        selected_before = curator.summary().policy_selected_segment_count
        decision = curator.evaluate(item)
        if decision.selected:
            curator.commit_selected(item, decision)
            if selected_before >= BALANCE_MIN_SELECTED_SEGMENTS:
                accepted_after_balance += 1
            if label == "rare":
                late_label_selected = True
        else:
            curator.record_rejected(item, decision)

    summary = curator.summary()
    return ProbeResult(
        scenario=scenario,
        hours=hours,
        candidates=candidate_count,
        selected_segments=summary.policy_selected_segment_count,
        selected_duration_sec=summary.policy_selected_duration_sec,
        selected_audio_bytes=summary.policy_selected_audio_bytes,
        rejected_repetitive=summary.rejected_repetitive_count,
        rejected_class_balance=summary.rejected_class_balance_count,
        rejected_session_budget=summary.rejected_session_budget_count,
        accepted_after_balance_enabled=accepted_after_balance,
        late_label_selected=late_label_selected,
    )


def _passes(result: ProbeResult) -> bool:
    bounded = (
        result.selected_segments <= 600
        and result.selected_duration_sec <= 3600
        and result.selected_audio_bytes <= 512 * 1024 * 1024
    )
    if result.scenario == "sustained":
        return bounded and result.rejected_repetitive > 0
    if result.scenario == "imbalanced":
        return (
            bounded
            and result.rejected_class_balance > 0
            and result.accepted_after_balance_enabled > 0
        )
    return bounded and result.late_label_selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise bounded curation policy.")
    parser.add_argument(
        "--scenario",
        choices=("sustained", "imbalanced", "rare-late"),
        required=True,
    )
    parser.add_argument("--hours", type=float, default=10.0)
    args = parser.parse_args()
    if not math.isfinite(args.hours) or args.hours <= 0:
        parser.error("--hours must be a positive finite number")

    result = run_probe(args.scenario, args.hours)
    passed = _passes(result)
    print(
        f"scenario={result.scenario} hours={result.hours:.1f} "
        f"candidates={result.candidates}"
    )
    print(
        f"selected_segments={result.selected_segments} "
        f"selected_duration_sec={result.selected_duration_sec:.1f} "
        f"selected_audio_bytes={result.selected_audio_bytes}"
    )
    if result.scenario == "sustained":
        print(
            f"rejected_repetitive={result.rejected_repetitive} "
            f"rejected_session_budget={result.rejected_session_budget} "
            f"limit_violations={0 if passed else 1}"
        )
    elif result.scenario == "imbalanced":
        print(
            f"accepted_after_balance_enabled={result.accepted_after_balance_enabled} "
            f"rejected_class_balance={result.rejected_class_balance} "
            f"selection_deadlock={'false' if result.accepted_after_balance_enabled else 'true'} "
            f"limit_violations={0 if passed else 1}"
        )
    else:
        print(
            f"late_label_selected={'true' if result.late_label_selected else 'false'} "
            f"limit_violations={0 if passed else 1}"
        )
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
