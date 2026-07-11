from backend.app.segment_files import (
    make_segment_stem,
    resolve_segment_audio,
    sorted_segment_metadata_paths,
)


def test_segment_metadata_paths_are_sorted_by_numeric_index(tmp_path):
    for index in (1001, 999, 1000):
        (tmp_path / f"segment-{index:03d}-0.000-1.000.json").write_text("{}")

    paths = sorted_segment_metadata_paths(tmp_path)

    assert [int(path.name.split("-")[1]) for path in paths] == [999, 1000, 1001]


def test_segment_file_helpers_make_stem_and_resolve_audio(tmp_path):
    stem = make_segment_stem(1, 0.0, 2.0)
    wav_path = tmp_path / f"{stem}.wav"
    wav_path.write_bytes(b"wav")

    assert stem == "segment-001-0.000-2.000"
    assert resolve_segment_audio(tmp_path, stem) == wav_path
