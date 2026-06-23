from backend.app.audio import (
    COCHL_SUPPORTED_CONTENT_TYPES,
    PreparedAudio,
    UploadTooLargeError,
    extension_for_content_type,
    is_cochl_supported_audio,
    prepare_audio_for_cochl,
    validate_upload_size,
)


def test_ogg_is_supported_by_cochl():
    assert "audio/ogg" in COCHL_SUPPORTED_CONTENT_TYPES
    assert is_cochl_supported_audio("audio/ogg", "recording.ogg") is True


def test_webm_requires_conversion():
    assert is_cochl_supported_audio("audio/webm", "recording.webm") is False
    assert extension_for_content_type("audio/webm") == ".webm"


def test_validate_upload_size_rejects_too_large_payload():
    try:
        validate_upload_size(26 * 1024 * 1024, 25)
    except UploadTooLargeError as exc:
        assert "25 MB" in str(exc)
    else:
        raise AssertionError("Expected upload size failure")


def test_prepare_audio_keeps_supported_file(tmp_path):
    source = tmp_path / "clip.ogg"
    source.write_bytes(b"fake")

    prepared = prepare_audio_for_cochl(source, "audio/ogg", "clip.ogg")

    assert prepared == PreparedAudio(
        path=source,
        content_type="audio/ogg",
    )


def test_prepare_audio_converts_unsupported_file(tmp_path, monkeypatch):
    source = tmp_path / "clip.webm"
    source.write_bytes(b"fake")
    converted_paths = []

    def fake_convert_to_wav(input_path, output_path):
        converted_paths.append((input_path, output_path))
        output_path.write_bytes(b"wav")

    monkeypatch.setattr("backend.app.audio.convert_to_wav", fake_convert_to_wav)

    prepared = prepare_audio_for_cochl(source, "audio/webm", "clip.webm")

    assert prepared.path.suffix == ".wav"
    assert prepared.content_type == "audio/wav"
    assert prepared.path.read_bytes() == b"wav"
    assert converted_paths == [(source, prepared.path)]
