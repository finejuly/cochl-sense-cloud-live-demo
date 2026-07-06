import json
import struct
import wave

from backend.app.collection import (
    CHUNK_COLLECTED,
    CHUNK_DISCARDED_SILENT,
    CHUNK_DISCARDED_SPEECH,
    CollectionPolicy,
    LiveCollectionManager,
    SegmentCollector,
    classify_chunk_events,
    is_privacy_sensitive_label,
    policy_from_settings,
)
from backend.app.config import Settings
from backend.app.models import SoundEvent

FRAMERATE = 100

POLICY = CollectionPolicy(
    confidence_threshold=0.5,
    exclude_label_keywords=("speech", "whisper", "sing"),
    max_segment_sec=20.0,
    reorder_hold_back_sec=100.0,
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
        collection_max_segment_sec=15.0,
        collection_exclude_label_keywords=("speech",),
    )

    policy = policy_from_settings(settings)

    assert policy.confidence_threshold == 0.7
    assert policy.max_segment_sec == 15.0
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
            "source_sequence_id": 7,
        }
    ]
    assert segment.labels == ["Knock"]
    session_summary = json.loads((output_dir / "session.json").read_text("utf-8"))
    assert session_summary["segment_count"] == 1


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
        max_segment_sec=4.0,
        reorder_hold_back_sec=100.0,
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
    ] == [(0.0, 4.0), (3.0, 5.0)]


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
        max_segment_sec=20.0,
        reorder_hold_back_sec=4.0,
    )
    collector = SegmentCollector("session-a", tmp_path / "collected", policy)

    _, silent_path = add_chunk(collector, chunks_dir, 1, 0, 2, [])
    add_chunk(collector, chunks_dir, 2, 1, 3, [event()])
    assert silent_path.exists()  # watermark 3 - 4 < 0 has not passed chunk-1 yet

    add_chunk(collector, chunks_dir, 3, 2, 4, [event()])

    # Watermark reached 4 - 4 = 0 >= chunk-1 start, so it was processed and deleted.
    assert not silent_path.exists()


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

    assert decision == CHUNK_COLLECTED
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
