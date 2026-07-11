import json
import struct
import wave
from pathlib import Path

from backend.app.collection import (
    CHUNK_COLLECTED,
    CHUNK_DISCARDED_LATE,
    CHUNK_DISCARDED_SILENT,
    CHUNK_DISCARDED_SPEECH,
    CollectionPolicy,
    LiveCollectionManager,
    SegmentCollector,
    classify_chunk_events,
    delete_collected_segment,
    delete_collected_session,
    is_privacy_sensitive_label,
    list_collected_sessions,
    policy_from_settings,
    safe_collected_session_dir,
)
from backend.app.config import Settings
from backend.app.curation import CurationPolicy
from backend.app.models import SoundEvent

FRAMERATE = 100

# min_segment_sec=0 disables context padding so structural tests stay focused;
# padding behavior is covered by the dedicated min-length tests below.
POLICY = CollectionPolicy(
    confidence_threshold=0.5,
    exclude_label_keywords=("speech", "whisper", "sing"),
    min_segment_sec=0.0,
    max_segment_sec=20.0,
    reorder_hold_back_sec=100.0,
    curation=CurationPolicy(repeat_cooldown_sec=0),
)

PADDING_POLICY = CollectionPolicy(
    confidence_threshold=0.5,
    exclude_label_keywords=("speech", "whisper", "sing"),
    min_segment_sec=5.0,
    max_segment_sec=20.0,
    reorder_hold_back_sec=100.0,
    curation=CurationPolicy(repeat_cooldown_sec=0),
)


def event(label="Keyboard", confidence=0.9, start=0.0, end=1.0):
    return SoundEvent(
        start_time_sec=start,
        end_time_sec=end,
        label=label,
        confidence=confidence,
    )


def write_ramp_chunk(path, start_sec, end_sec, framerate=FRAMERATE):
    """Writes 16-bit mono PCM whose sample values equal global frame indexes."""
    start_frame = round(start_sec * framerate)
    frame_count = round((end_sec - start_sec) * framerate)
    values = range(start_frame, start_frame + frame_count)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(framerate)
        writer.writeframes(struct.pack(f"<{frame_count}h", *values))


def read_wav_values(path):
    with wave.open(str(path), "rb") as reader:
        frames = reader.readframes(reader.getnframes())
    return list(struct.unpack(f"<{len(frames) // 2}h", frames))


def add_chunk(collector, chunks_dir, sequence_id, start_sec, end_sec, events):
    wav_path = chunks_dir / f"chunk-{sequence_id:06d}.wav"
    write_ramp_chunk(wav_path, start_sec, end_sec)
    decision = collector.add_chunk(
        sequence_id=sequence_id,
        window_start_sec=start_sec,
        window_end_sec=end_sec,
        wav_path=wav_path,
        events=events,
    )
    return decision, wav_path


def test_classify_keeps_confident_non_speech_events():
    events = [event("Keyboard", 0.72)]

    assert classify_chunk_events(events, POLICY) == CHUNK_COLLECTED


def test_classify_discards_low_confidence_as_silent():
    events = [event("Keyboard", 0.2)]

    assert classify_chunk_events(events, POLICY) == CHUNK_DISCARDED_SILENT


def test_classify_discards_empty_results_as_silent():
    assert classify_chunk_events([], POLICY) == CHUNK_DISCARDED_SILENT


def test_classify_privacy_wins_over_other_events():
    events = [event("Keyboard", 0.9), event("Male_speech", 0.1)]

    assert classify_chunk_events(events, POLICY) == CHUNK_DISCARDED_SPEECH


def test_classify_keeps_events_without_confidence():
    events = [event("Glass_break", None)]

    assert classify_chunk_events(events, POLICY) == CHUNK_COLLECTED


def test_privacy_label_matching_is_substring_and_case_insensitive():
    keywords = ("speech", "whisper", "sing")

    assert is_privacy_sensitive_label("Male_speech", keywords)
    assert is_privacy_sensitive_label("Whispering", keywords)
    assert is_privacy_sensitive_label("Singing", keywords)
    assert not is_privacy_sensitive_label("Knock", keywords)


def test_policy_from_settings_maps_collection_fields():
    settings = Settings(
        cochl_project_key="key",
        collection_confidence_threshold=0.7,
        collection_min_segment_sec=4.0,
        collection_max_segment_sec=15.0,
        collection_silence_close_sec=2.0,
        collection_exclude_label_keywords=("speech",),
    )

    policy = policy_from_settings(settings)

    assert policy.confidence_threshold == 0.7
    assert policy.min_segment_sec == 4.0
    assert policy.max_segment_sec == 15.0
    assert policy.silence_close_sec == 2.0
    assert policy.exclude_label_keywords == ("speech",)


def test_collector_merges_overlapping_chunks_into_one_segment(tmp_path):
    chunks_dir = tmp_path / "live"
    output_dir = tmp_path / "collected"
    collector = SegmentCollector("session-a", output_dir, POLICY)

    paths = []
    for sequence_id, (start, end) in enumerate([(0, 2), (1, 3), (2, 4)], start=1):
        decision, wav_path = add_chunk(
            collector, chunks_dir, sequence_id, start, end, [event()]
        )
        assert decision == CHUNK_COLLECTED
        paths.append(wav_path)

    summary = collector.end_session()

    assert summary.segment_count == 1
    assert summary.kept_chunk_count == 3
    segment = summary.segments[0]
    assert segment.start_sec == 0.0
    assert segment.end_sec == 4.0
    assert segment.duration_sec == 4.0
    segment_path = output_dir / segment.audio_filename
    assert read_wav_values(segment_path) == list(range(4 * FRAMERATE))
    assert all(not path.exists() for path in paths)


def test_collector_writes_segment_metadata(tmp_path):
    output_dir = tmp_path / "collected"
    collector = SegmentCollector("session-a", output_dir, POLICY)
    add_chunk(
        collector,
        tmp_path / "live",
        7,
        4.0,
        6.0,
        [event("Knock", 0.8, start=4.5, end=5.0)],
    )

    summary = collector.end_session()

    segment = summary.segments[0]
    metadata = json.loads((output_dir / segment.metadata_filename).read_text("utf-8"))
    assert metadata["session_id"] == "session-a"
    assert metadata["segment_index"] == 1
    assert metadata["start_sec"] == 4.0
    assert metadata["end_sec"] == 6.0
    assert metadata["sample_rate"] == FRAMERATE
    assert metadata["audio_filename"] == segment.audio_filename
    assert metadata["chunk_sequence_ids"] == [7]
    assert metadata["events"] == [
        {
            "start_time_sec": 4.5,
            "end_time_sec": 5.0,
            "label": "Knock",
            "confidence": 0.8,
            "supporting_window_count": 1,
        }
    ]
    assert segment.labels == ["Knock"]
    session_summary = json.loads((output_dir / "session.json").read_text("utf-8"))
    assert session_summary["segment_count"] == 1
    assert session_summary["candidate_segment_count"] == 1
    assert session_summary["policy_selected_segment_count"] == 1
    assert "segments" not in session_summary


def test_collector_journals_repetitive_rejection_but_not_selected(tmp_path):
    policy = CollectionPolicy(
        confidence_threshold=0.5,
        exclude_label_keywords=("speech",),
        min_segment_sec=0.0,
        max_segment_sec=20.0,
        reorder_hold_back_sec=100.0,
        curation=CurationPolicy(repeat_cooldown_sec=600),
    )
    output_dir = tmp_path / "collected"
    collector = SegmentCollector("session-a", output_dir, policy)
    add_chunk(collector, tmp_path / "live", 1, 0, 2, [event()])
    add_chunk(collector, tmp_path / "live", 2, 4, 6, [event(start=4, end=5)])

    summary = collector.end_session()
    decisions = [
        json.loads(line)
        for line in (output_dir / "decisions.jsonl").read_text("utf-8").splitlines()
    ]

    assert summary.candidate_segment_count == 2
    assert summary.segment_count == 1
    assert summary.rejected_repetitive_count == 1
    assert [decision["reason"] for decision in decisions] == ["repetitive"]


def test_collector_rejects_mismatched_audio_as_invalid(tmp_path):
    policy = CollectionPolicy(
        confidence_threshold=0.5,
        exclude_label_keywords=("speech",),
        min_segment_sec=0.0,
        max_segment_sec=20.0,
        reorder_hold_back_sec=100.0,
    )
    output_dir = tmp_path / "collected"
    chunks_dir = tmp_path / "live"
    collector = SegmentCollector("session-a", output_dir, policy)
    first = chunks_dir / "chunk-000001.wav"
    second = chunks_dir / "chunk-000002.wav"
    write_ramp_chunk(first, 0, 2, framerate=100)
    write_ramp_chunk(second, 1, 3, framerate=200)
    collector.add_chunk(
        sequence_id=1,
        window_start_sec=0,
        window_end_sec=2,
        wav_path=first,
        events=[event()],
    )
    collector.add_chunk(
        sequence_id=2,
        window_start_sec=1,
        window_end_sec=3,
        wav_path=second,
        events=[event(start=1, end=2)],
    )

    summary = collector.end_session()
    decision = json.loads(
        (output_dir / "decisions.jsonl").read_text("utf-8").splitlines()[0]
    )

    assert summary.segment_count == 0
    assert summary.invalid_audio_count == 1
    assert decision["reason"] == "invalid_audio"
    assert decision["skipped_sequence_ids"] == [2]
    assert not first.exists()
    assert not second.exists()


def test_all_rejected_session_is_listed_with_zero_segments(tmp_path):
    output_dir = tmp_path / "collected" / "session-a"
    policy = CollectionPolicy(
        confidence_threshold=0.5,
        exclude_label_keywords=("speech",),
        min_segment_sec=0.0,
        max_segment_sec=20.0,
        reorder_hold_back_sec=100.0,
        curation=CurationPolicy(max_duration_sec=1.0),
    )
    collector = SegmentCollector("session-a", output_dir, policy)
    add_chunk(collector, tmp_path / "live", 1, 0, 2, [event()])

    summary = collector.end_session()
    listed = list_collected_sessions(tmp_path / "collected")

    assert summary.segment_count == 0
    assert summary.rejected_session_budget_count == 1
    assert len(listed) == 1
    assert listed[0].segment_count == 0
    assert listed[0].candidate_segment_count == 1


def test_write_failure_rolls_back_pair_and_records_terminal_outcome(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "collected"
    collector = SegmentCollector("session-a", output_dir, POLICY)
    _, source = add_chunk(collector, tmp_path / "live", 1, 0, 2, [event()])
    original_write_text = Path.write_text

    def fail_segment_metadata(path, *args, **kwargs):
        if path.name.startswith(".segment-") and path.name.endswith(".json.tmp"):
            raise OSError("injected metadata failure")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_segment_metadata)

    summary = collector.end_session()
    decisions = [
        json.loads(line)
        for line in (output_dir / "decisions.jsonl").read_text("utf-8").splitlines()
    ]

    assert summary.segment_count == 0
    assert summary.write_error_count == 1
    assert decisions[0]["reason"] == "write_error"
    assert not list(output_dir.glob("segment-*"))
    assert not source.exists()


def test_journal_append_failure_keeps_reject_count_and_cleanup(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "collected"
    policy = CollectionPolicy(
        confidence_threshold=0.5,
        exclude_label_keywords=("speech",),
        min_segment_sec=0.0,
        max_segment_sec=20.0,
        reorder_hold_back_sec=100.0,
        curation=CurationPolicy(max_duration_sec=1.0),
    )
    collector = SegmentCollector("session-a", output_dir, policy)
    _, source = add_chunk(collector, tmp_path / "live", 1, 0, 2, [event()])
    original_open = Path.open

    def fail_journal(path, *args, **kwargs):
        if path.name == "decisions.jsonl":
            raise OSError("injected journal failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_journal)

    summary = collector.end_session()

    assert summary.rejected_session_budget_count == 1
    assert summary.segment_count == 0
    assert not source.exists()


def test_collector_splits_segments_on_time_gap(tmp_path):
    chunks_dir = tmp_path / "live"
    collector = SegmentCollector("session-a", tmp_path / "collected", POLICY)

    add_chunk(collector, chunks_dir, 1, 0, 2, [event()])
    _, silent_path = add_chunk(collector, chunks_dir, 2, 1, 3, [])
    add_chunk(collector, chunks_dir, 3, 4, 6, [event()])

    summary = collector.end_session()

    assert summary.segment_count == 2
    assert summary.discarded_silent_chunk_count == 1
    assert [
        (segment.start_sec, segment.end_sec) for segment in summary.segments
    ] == [(0.0, 2.0), (4.0, 6.0)]
    assert not silent_path.exists()


def test_collector_merges_nearby_bursts_through_buffered_silence(tmp_path):
    chunks_dir = tmp_path / "live"
    output_dir = tmp_path / "collected"
    policy = CollectionPolicy(
        confidence_threshold=0.5,
        exclude_label_keywords=("speech",),
        min_segment_sec=5.0,
        max_segment_sec=20.0,
        silence_close_sec=5.0,
        reorder_hold_back_sec=100.0,
    )
    collector = SegmentCollector("session-a", output_dir, policy)

    for sequence_id, (start, end) in enumerate(
        [(0, 2), (1, 3), (2, 4)], start=1
    ):
        add_chunk(collector, chunks_dir, sequence_id, start, end, [event()])
    add_chunk(collector, chunks_dir, 4, 3, 5, [])
    add_chunk(collector, chunks_dir, 5, 4, 6, [])
    add_chunk(collector, chunks_dir, 6, 5, 7, [])
    add_chunk(collector, chunks_dir, 7, 6, 8, [event("Knock")])

    summary = collector.end_session()

    assert summary.segment_count == 1
    assert summary.segments[0].start_sec == 0.0
    assert summary.segments[0].end_sec == 8.0
    assert read_wav_values(output_dir / summary.segments[0].audio_filename) == list(
        range(8 * FRAMERATE)
    )


def test_collector_keeps_bursts_separate_after_silence_timeout(tmp_path):
    chunks_dir = tmp_path / "live"
    policy = CollectionPolicy(
        confidence_threshold=0.5,
        exclude_label_keywords=("speech",),
        min_segment_sec=5.0,
        max_segment_sec=20.0,
        silence_close_sec=5.0,
        reorder_hold_back_sec=100.0,
    )
    collector = SegmentCollector("session-a", tmp_path / "collected", policy)

    add_chunk(collector, chunks_dir, 1, 0, 2, [event("Keyboard")])
    for sequence_id, (start, end) in enumerate(
        [(1, 3), (2, 4), (3, 5), (4, 6), (5, 7), (6, 8), (7, 9)],
        start=2,
    ):
        add_chunk(collector, chunks_dir, sequence_id, start, end, [])
    add_chunk(collector, chunks_dir, 9, 8, 10, [event("Knock")])

    summary = collector.end_session()

    assert summary.segment_count == 2
    assert summary.segments[0].labels == ["Keyboard"]
    assert summary.segments[1].labels == ["Knock"]


def test_collector_splits_segments_at_speech_even_when_contiguous(tmp_path):
    chunks_dir = tmp_path / "live"
    collector = SegmentCollector("session-a", tmp_path / "collected", POLICY)

    add_chunk(collector, chunks_dir, 1, 0, 2, [event()])
    decision, speech_path = add_chunk(
        collector, chunks_dir, 2, 1, 3, [event("Speech", 0.9)]
    )
    add_chunk(collector, chunks_dir, 3, 2, 4, [event()])

    summary = collector.end_session()

    assert decision == CHUNK_DISCARDED_SPEECH
    assert summary.segment_count == 2
    assert summary.discarded_speech_chunk_count == 1
    assert [
        (segment.start_sec, segment.end_sec) for segment in summary.segments
    ] == [(0.0, 2.0), (2.0, 4.0)]
    assert not speech_path.exists()


def test_collector_splits_segments_at_max_duration(tmp_path):
    chunks_dir = tmp_path / "live"
    policy = CollectionPolicy(
        confidence_threshold=0.5,
        exclude_label_keywords=("speech",),
        min_segment_sec=0.0,
        max_segment_sec=4.0,
        reorder_hold_back_sec=100.0,
        curation=CurationPolicy(repeat_cooldown_sec=0),
    )
    collector = SegmentCollector("session-a", tmp_path / "collected", policy)

    for sequence_id, (start, end) in enumerate(
        [(0, 2), (1, 3), (2, 4), (3, 5)], start=1
    ):
        add_chunk(collector, chunks_dir, sequence_id, start, end, [event()])

    summary = collector.end_session()

    assert summary.segment_count == 2
    assert [
        (segment.start_sec, segment.end_sec) for segment in summary.segments
    ] == [(0.0, 3.0), (3.0, 5.0)]
    first = read_wav_values(tmp_path / "collected" / summary.segments[0].audio_filename)
    second = read_wav_values(tmp_path / "collected" / summary.segments[1].audio_filename)
    assert first + second == list(range(500))


def test_collector_orders_out_of_order_chunks_before_merging(tmp_path):
    chunks_dir = tmp_path / "live"
    output_dir = tmp_path / "collected"
    collector = SegmentCollector("session-a", output_dir, POLICY)

    add_chunk(collector, chunks_dir, 2, 1, 3, [event()])
    add_chunk(collector, chunks_dir, 1, 0, 2, [event()])
    add_chunk(collector, chunks_dir, 3, 2, 4, [event()])

    summary = collector.end_session()

    assert summary.segment_count == 1
    segment = summary.segments[0]
    segment_path = output_dir / segment.audio_filename
    assert read_wav_values(segment_path) == list(range(4 * FRAMERATE))


def test_collector_flushes_pending_chunks_once_watermark_passes(tmp_path):
    chunks_dir = tmp_path / "live"
    policy = CollectionPolicy(
        confidence_threshold=0.5,
        exclude_label_keywords=("speech",),
        min_segment_sec=0.0,
        max_segment_sec=20.0,
        reorder_hold_back_sec=4.0,
    )
    collector = SegmentCollector("session-a", tmp_path / "collected", policy)

    _, speech_path = add_chunk(collector, chunks_dir, 1, 0, 2, [event("Speech", 0.9)])
    add_chunk(collector, chunks_dir, 2, 1, 3, [event()])
    assert speech_path.exists()  # watermark 3 - 4 < 0 has not passed chunk-1 yet

    add_chunk(collector, chunks_dir, 3, 2, 4, [event()])

    # Watermark reached 4 - 4 = 0 >= chunk-1 start, so it was processed and deleted.
    assert not speech_path.exists()


def test_short_signal_gets_leading_background_context(tmp_path):
    chunks_dir = tmp_path / "live"
    output_dir = tmp_path / "collected"
    collector = SegmentCollector("session-a", output_dir, PADDING_POLICY)

    add_chunk(collector, chunks_dir, 1, 0, 2, [])
    add_chunk(collector, chunks_dir, 2, 1, 3, [])
    add_chunk(collector, chunks_dir, 3, 2, 4, [])
    add_chunk(collector, chunks_dir, 4, 3, 5, [event("Glass_break", 0.9)])

    summary = collector.end_session()

    assert summary.segment_count == 1
    segment = summary.segments[0]
    # A 2-second detection is padded backwards with silent context to >= 5s.
    assert segment.start_sec == 0.0
    assert segment.end_sec == 5.0
    assert segment.duration_sec == 5.0
    assert segment.labels == ["Glass_break"]
    segment_path = output_dir / segment.audio_filename
    assert read_wav_values(segment_path) == list(range(5 * FRAMERATE))
    assert summary.discarded_silent_chunk_count == 0


def test_short_signal_gets_trailing_background_context(tmp_path):
    chunks_dir = tmp_path / "live"
    output_dir = tmp_path / "collected"
    collector = SegmentCollector("session-a", output_dir, PADDING_POLICY)

    add_chunk(collector, chunks_dir, 1, 0, 2, [event("Glass_break", 0.9)])
    for sequence_id, (start, end) in enumerate(
        [(1, 3), (2, 4), (3, 5), (4, 6), (5, 7)], start=2
    ):
        add_chunk(collector, chunks_dir, sequence_id, start, end, [])

    summary = collector.end_session()

    assert summary.segment_count == 1
    segment = summary.segments[0]
    # Trailing silent chunks extend the segment until the 5-second minimum.
    assert segment.start_sec == 0.0
    assert segment.end_sec == 5.0
    segment_path = output_dir / segment.audio_filename
    assert read_wav_values(segment_path) == list(range(5 * FRAMERATE))
    # The remaining buffered context was not needed and is discarded.
    assert summary.discarded_silent_chunk_count == 2


def test_short_signal_gets_context_on_both_sides(tmp_path):
    chunks_dir = tmp_path / "live"
    output_dir = tmp_path / "collected"
    collector = SegmentCollector("session-a", output_dir, PADDING_POLICY)

    add_chunk(collector, chunks_dir, 1, 0, 2, [])
    add_chunk(collector, chunks_dir, 2, 1, 3, [])
    add_chunk(collector, chunks_dir, 3, 2, 4, [])
    add_chunk(collector, chunks_dir, 4, 3, 5, [event("Glass_break", 0.9)])
    add_chunk(collector, chunks_dir, 5, 4, 6, [])
    add_chunk(collector, chunks_dir, 6, 5, 7, [])

    summary = collector.end_session()

    assert summary.segment_count == 1
    segment = summary.segments[0]
    # The 2s detection at 3-5s is padded on BOTH sides: leading pre-roll is
    # capped at half the deficit so trailing silence covers the rest.
    assert segment.start_sec == 1.0
    assert segment.end_sec == 6.0
    assert segment.duration_sec == 5.0
    segment_path = output_dir / segment.audio_filename
    assert read_wav_values(segment_path) == list(range(1 * FRAMERATE, 6 * FRAMERATE))
    assert summary.discarded_silent_chunk_count == 2


def test_context_padding_never_crosses_a_speech_boundary(tmp_path):
    chunks_dir = tmp_path / "live"
    collector = SegmentCollector("session-a", tmp_path / "collected", PADDING_POLICY)

    add_chunk(collector, chunks_dir, 1, 0, 2, [])
    _, speech_path = add_chunk(collector, chunks_dir, 2, 1, 3, [event("Speech", 0.9)])
    add_chunk(collector, chunks_dir, 3, 2, 4, [])
    add_chunk(collector, chunks_dir, 4, 3, 5, [event("Glass_break", 0.9)])

    summary = collector.end_session()

    assert summary.segment_count == 1
    segment = summary.segments[0]
    # Pre-roll may only reach back to the silent chunk after the speech chunk.
    assert segment.start_sec == 2.0
    assert segment.end_sec == 5.0
    assert not speech_path.exists()
    assert summary.discarded_speech_chunk_count == 1
    assert summary.discarded_silent_chunk_count == 1


def test_sustained_silence_finalizes_the_segment_in_real_time(tmp_path):
    chunks_dir = tmp_path / "live"
    output_dir = tmp_path / "collected"
    # No reorder hold-back so chunks process as they arrive, like a live
    # session whose watermark has caught up.
    realtime_policy = CollectionPolicy(
        confidence_threshold=0.5,
        exclude_label_keywords=("speech",),
        min_segment_sec=5.0,
        max_segment_sec=20.0,
        silence_close_sec=3.0,
        reorder_hold_back_sec=0.0,
    )
    collector = SegmentCollector("session-a", output_dir, realtime_policy)

    add_chunk(collector, chunks_dir, 1, 0, 2, [event("Glass_break", 0.9)])
    for sequence_id, (start, end) in enumerate(
        [(1, 3), (2, 4), (3, 5), (4, 6)], start=2
    ):
        add_chunk(collector, chunks_dir, sequence_id, start, end, [])

    # 3+ seconds of silence past the detection: the file exists BEFORE the
    # session ends, and session.json already carries name/start metadata.
    segment_files = list(output_dir.glob("segment-*.wav"))
    assert len(segment_files) == 1
    partial_summary = json.loads((output_dir / "session.json").read_text("utf-8"))
    assert partial_summary["segment_count"] == 1
    assert partial_summary["ended_at"] is None

    summary = collector.end_session()

    assert summary.segment_count == 1
    assert summary.segments[0].start_sec == 0.0
    assert summary.segments[0].end_sec == 5.0
    final_summary = json.loads((output_dir / "session.json").read_text("utf-8"))
    assert final_summary["ended_at"] is not None


def test_brief_lull_does_not_split_an_ongoing_detection(tmp_path):
    chunks_dir = tmp_path / "live"
    collector = SegmentCollector("session-a", tmp_path / "collected", PADDING_POLICY)

    for sequence_id, (start, end) in enumerate(
        [(0, 2), (1, 3), (2, 4), (3, 5), (4, 6)], start=1
    ):
        add_chunk(collector, chunks_dir, sequence_id, start, end, [event()])
    add_chunk(collector, chunks_dir, 6, 5, 7, [])  # one silent window (lull)
    add_chunk(collector, chunks_dir, 7, 6, 8, [event()])

    summary = collector.end_session()

    assert summary.segment_count == 1
    assert summary.segments[0].start_sec == 0.0
    assert summary.segments[0].end_sec == 8.0


def test_long_detection_is_not_padded_beyond_minimum(tmp_path):
    chunks_dir = tmp_path / "live"
    collector = SegmentCollector("session-a", tmp_path / "collected", PADDING_POLICY)

    add_chunk(collector, chunks_dir, 1, 0, 2, [])
    for sequence_id, (start, end) in enumerate(
        [(1, 3), (2, 4), (3, 5), (4, 6), (5, 7)], start=2
    ):
        add_chunk(collector, chunks_dir, sequence_id, start, end, [event()])

    summary = collector.end_session()

    assert summary.segment_count == 1
    segment = summary.segments[0]
    # Already >= 5s of meaningful audio: only the immediate pre-roll is added.
    assert segment.end_sec == 7.0
    assert segment.duration_sec >= 5.0


def test_collector_invokes_mp3_scheduler_for_finalized_segments(tmp_path):
    scheduled = []
    collector = SegmentCollector(
        "session-a",
        tmp_path / "collected",
        POLICY,
        mp3_scheduler=scheduled.append,
    )
    add_chunk(collector, tmp_path / "live", 1, 0, 2, [event()])

    summary = collector.end_session()

    assert scheduled == [tmp_path / "collected" / summary.segments[0].audio_filename]


def test_collector_deletes_late_chunks_after_session_end(tmp_path):
    chunks_dir = tmp_path / "live"
    collector = SegmentCollector("session-a", tmp_path / "collected", POLICY)
    collector.end_session()

    decision, late_path = add_chunk(collector, chunks_dir, 9, 8, 10, [event()])

    assert decision == CHUNK_DISCARDED_LATE
    assert not late_path.exists()


def test_collector_removes_empty_live_dir_on_end(tmp_path):
    chunks_dir = tmp_path / "live" / "session-a"
    collector = SegmentCollector("session-a", tmp_path / "collected", POLICY)
    add_chunk(collector, chunks_dir, 1, 0, 2, [event()])

    collector.end_session()

    assert not chunks_dir.exists()


def test_manager_routes_chunks_and_ends_sessions(tmp_path):
    manager = LiveCollectionManager()
    chunks_dir = tmp_path / "live" / "session-a"
    wav_path = chunks_dir / "chunk-000001.wav"
    write_ramp_chunk(wav_path, 0, 2)

    decision = manager.add_chunk(
        "session-a",
        output_dir=tmp_path / "collected" / "session-a",
        policy=POLICY,
        sequence_id=1,
        window_start_sec=0.0,
        window_end_sec=2.0,
        wav_path=wav_path,
        events=[event()],
    )
    summary = manager.end_session("session-a")

    assert decision == CHUNK_COLLECTED
    assert summary.segment_count == 1
    assert summary.kept_chunk_count == 1


def test_manager_end_unknown_session_returns_empty_summary():
    manager = LiveCollectionManager()

    summary = manager.end_session("missing")

    assert summary.segment_count == 0
    assert summary.kept_chunk_count == 0
    assert summary.segments == []


def test_manager_deletes_late_chunks_for_ended_sessions(tmp_path):
    manager = LiveCollectionManager()
    manager.end_session("session-a")

    late_wav = tmp_path / "live" / "session-a" / "chunk-000009.wav"
    write_ramp_chunk(late_wav, 8, 10)
    decision = manager.add_chunk(
        "session-a",
        output_dir=tmp_path / "collected" / "session-a",
        policy=POLICY,
        sequence_id=9,
        window_start_sec=8.0,
        window_end_sec=10.0,
        wav_path=late_wav,
        events=[event()],
    )

    assert decision == CHUNK_DISCARDED_LATE
    assert not late_wav.exists()
    assert not late_wav.parent.exists()
    assert "session-a" not in manager._collectors


def test_collector_records_session_name_and_timestamps(tmp_path):
    output_dir = tmp_path / "collected"
    collector = SegmentCollector(
        "session-a",
        output_dir,
        POLICY,
        session_name="사무실 소음",
    )
    add_chunk(collector, tmp_path / "live", 1, 0, 2, [event()])

    summary = collector.end_session()

    assert summary.session_name == "사무실 소음"
    assert summary.started_at is not None
    assert summary.ended_at is not None
    assert summary.ended_at >= summary.started_at
    session_summary = json.loads((output_dir / "session.json").read_text("utf-8"))
    assert session_summary["session_name"] == "사무실 소음"
    assert session_summary["started_at"] == summary.started_at
    segment = summary.segments[0]
    metadata = json.loads((output_dir / segment.metadata_filename).read_text("utf-8"))
    assert metadata["session_name"] == "사무실 소음"
    assert metadata["session_started_at"] == summary.started_at


def test_manager_applies_session_name_from_chunk_or_end(tmp_path):
    manager = LiveCollectionManager()
    wav_path = tmp_path / "live" / "session-a" / "chunk-000001.wav"
    write_ramp_chunk(wav_path, 0, 2)
    manager.add_chunk(
        "session-a",
        output_dir=tmp_path / "collected" / "session-a",
        policy=POLICY,
        session_name=None,
        sequence_id=1,
        window_start_sec=0.0,
        window_end_sec=2.0,
        wav_path=wav_path,
        events=[event()],
    )

    summary = manager.end_session("session-a", session_name="지하철 플랫폼")

    assert summary.session_name == "지하철 플랫폼"


def make_collected_session(tmp_path, session_id, name=None, started_at=None):
    collector = SegmentCollector(
        session_id,
        tmp_path / "collected" / session_id,
        POLICY,
        session_name=name,
    )
    if started_at is not None:
        collector.started_at = started_at
    add_chunk(collector, tmp_path / "live" / session_id, 1, 0, 2, [event("Knock", 0.8)])
    return collector.end_session()


def test_list_collected_sessions_reads_metadata_from_disk(tmp_path):
    from datetime import datetime, timezone

    make_collected_session(
        tmp_path,
        "session-old",
        name="옛 세션",
        started_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    make_collected_session(
        tmp_path,
        "session-new",
        name="새 세션",
        started_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
    )

    sessions = list_collected_sessions(tmp_path / "collected")

    assert [session.session_id for session in sessions] == [
        "session-new",
        "session-old",
    ]
    newest = sessions[0]
    assert newest.session_name == "새 세션"
    assert newest.started_at is not None
    assert newest.segment_count == 1
    assert newest.segments[0].labels == ["Knock"]
    assert newest.segments[0].audio_filename.endswith(".wav")


def test_list_collected_sessions_restores_gcs_upload_status(tmp_path):
    make_collected_session(tmp_path, "session-uploaded", name="업로드 완료")
    marker = tmp_path / "collected" / "session-uploaded" / ".gcs-upload.json"
    marker.write_text(
        json.dumps(
            {
                "status": "uploaded",
                "object_prefix": "root/install/session/snapshot",
                "snapshot_id": "snapshot",
                "uploaded_at": "2026-07-10T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    session = list_collected_sessions(tmp_path / "collected")[0]

    assert session.gcs_upload is not None
    assert session.gcs_upload.status == "uploaded"
    assert session.gcs_upload.snapshot_id == "snapshot"


def test_list_collected_sessions_handles_missing_root(tmp_path):
    assert list_collected_sessions(tmp_path / "missing") == []


def test_safe_collected_session_dir_rejects_traversal(tmp_path):
    collected = tmp_path / "collected"
    (collected / "session-a").mkdir(parents=True)
    (tmp_path / "secret").mkdir()

    assert safe_collected_session_dir(collected, "session-a") is not None
    assert safe_collected_session_dir(collected, "../secret") is None
    assert safe_collected_session_dir(collected, "missing") is None


def test_delete_collected_session_removes_directory(tmp_path):
    make_collected_session(tmp_path, "session-a")
    collected = tmp_path / "collected"

    assert delete_collected_session(collected, "session-a") is True
    assert not (collected / "session-a").exists()
    assert delete_collected_session(collected, "session-a") is False
    assert delete_collected_session(collected, "../session-a") is False


def test_delete_collected_segment_preserves_empty_curated_session(tmp_path):
    summary = make_collected_session(tmp_path, "session-a")
    collected = tmp_path / "collected"
    segment = summary.segments[0]

    assert delete_collected_segment(collected, "session-a", segment.audio_filename)
    assert (collected / "session-a").exists()
    session = list_collected_sessions(collected)[0]
    assert session.segment_count == 0
    assert session.policy_selected_segment_count == 1


def test_delete_collected_segment_rejects_bad_names(tmp_path):
    make_collected_session(tmp_path, "session-a")
    collected = tmp_path / "collected"

    assert delete_collected_segment(collected, "session-a", "../session.json") is False
    assert delete_collected_segment(collected, "session-a", "session.json") is False
    assert delete_collected_segment(collected, "session-a", "missing.wav") is False
    assert (collected / "session-a").exists()


def test_manager_finalizes_stale_sessions_on_new_activity(tmp_path):
    manager = LiveCollectionManager(stale_session_sec=60.0)
    stale_dir = tmp_path / "live" / "stale"
    stale_wav = stale_dir / "chunk-000001.wav"
    write_ramp_chunk(stale_wav, 0, 2)
    manager.add_chunk(
        "stale",
        output_dir=tmp_path / "collected" / "stale",
        policy=POLICY,
        sequence_id=1,
        window_start_sec=0.0,
        window_end_sec=2.0,
        wav_path=stale_wav,
        events=[event()],
    )
    manager._collectors["stale"].last_activity_monotonic -= 120.0

    fresh_wav = tmp_path / "live" / "fresh" / "chunk-000001.wav"
    write_ramp_chunk(fresh_wav, 0, 2)
    manager.add_chunk(
        "fresh",
        output_dir=tmp_path / "collected" / "fresh",
        policy=POLICY,
        sequence_id=1,
        window_start_sec=0.0,
        window_end_sec=2.0,
        wav_path=fresh_wav,
        events=[event()],
    )

    assert "stale" not in manager._collectors
    assert (tmp_path / "collected" / "stale" / "session.json").exists()
    assert not stale_wav.exists()
