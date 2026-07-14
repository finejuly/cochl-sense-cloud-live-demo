from backend.app.audio import (
    COCHL_SUPPORTED_CONTENT_TYPES,
    LIVE_FFMPEG_TIMEOUT_SEC,
    LIVE_PROVIDER_VORBIS_QUALITY,
    AudioConversionError,
    PreparedAudio,
    UploadTooLargeError,
    convert_live_to_ogg,
    convert_to_mp3,
    extension_for_content_type,
    find_ffmpeg_executable,
    is_cochl_supported_audio,
    prepare_audio_for_cochl,
    prepare_live_audio_for_cochl,
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
    existing_wav = tmp_path / "clip.wav"
    existing_wav.write_bytes(b"existing")
    converted_paths = []

    def fake_convert_to_wav(input_path, output_path):
        converted_paths.append((input_path, output_path))
        output_path.write_bytes(b"wav")

    monkeypatch.setattr("backend.app.audio.convert_to_wav", fake_convert_to_wav)

    prepared = prepare_audio_for_cochl(source, "audio/webm", "clip.webm")

    assert prepared.path.suffix == ".wav"
    assert prepared.path != existing_wav
    assert prepared.path.name.startswith(".clip.webm.")
    assert prepared.path.name.endswith(".cochl.wav")
    assert prepared.content_type == "audio/wav"
    assert prepared.path.read_bytes() == b"wav"
    assert existing_wav.read_bytes() == b"existing"
    assert converted_paths == [(source, prepared.path)]


def test_find_ffmpeg_uses_executable_fallback_when_path_is_minimal(
    tmp_path, monkeypatch
):
    fallback = tmp_path / "ffmpeg"
    fallback.write_bytes(b"binary")
    fallback.chmod(0o755)
    monkeypatch.setattr("backend.app.audio.shutil.which", lambda name: None)
    monkeypatch.setattr("backend.app.audio.FFMPEG_FALLBACK_PATHS", (fallback,))

    assert find_ffmpeg_executable() == str(fallback)


def test_prepare_audio_removes_partial_conversion_on_failure(tmp_path, monkeypatch):
    source = tmp_path / "clip.webm"
    source.write_bytes(b"fake")
    attempted_output = None

    def failing_conversion(input_path, output_path):
        nonlocal attempted_output
        attempted_output = output_path
        output_path.write_bytes(b"partial")
        raise RuntimeError("conversion failed")

    monkeypatch.setattr("backend.app.audio.convert_to_wav", failing_conversion)

    try:
        prepare_audio_for_cochl(source, "audio/webm", source.name)
    except RuntimeError as exc:
        assert "conversion failed" in str(exc)
    else:
        raise AssertionError("Expected conversion failure")

    assert attempted_output is not None
    assert not attempted_output.exists()
    assert source.read_bytes() == b"fake"


def test_prepare_live_audio_creates_provider_only_ogg(tmp_path, monkeypatch):
    source = tmp_path / "chunk.wav"
    source.write_bytes(b"captured-wav")
    converted_paths = []

    def fake_convert(input_path, output_path):
        converted_paths.append((input_path, output_path))
        output_path.write_bytes(b"transport-ogg")

    monkeypatch.setattr("backend.app.audio.convert_live_to_ogg", fake_convert)

    prepared = prepare_live_audio_for_cochl(source)

    assert prepared.path != source
    assert prepared.path.name.startswith(".chunk.wav.")
    assert prepared.path.name.endswith(".cochl.ogg")
    assert prepared.content_type == "audio/ogg"
    assert prepared.path.read_bytes() == b"transport-ogg"
    assert source.read_bytes() == b"captured-wav"
    assert converted_paths == [(source, prepared.path)]


def test_prepare_live_audio_falls_back_and_removes_partial_ogg(tmp_path, monkeypatch):
    source = tmp_path / "chunk.wav"
    source.write_bytes(b"captured-wav")
    attempted_output = None

    def failing_conversion(input_path, output_path):
        nonlocal attempted_output
        attempted_output = output_path
        output_path.write_bytes(b"partial")
        raise AudioConversionError("ffmpeg unavailable")

    monkeypatch.setattr("backend.app.audio.convert_live_to_ogg", failing_conversion)

    prepared = prepare_live_audio_for_cochl(source)

    assert prepared == PreparedAudio(path=source, content_type="audio/wav")
    assert attempted_output is not None
    assert not attempted_output.exists()
    assert source.read_bytes() == b"captured-wav"


def test_convert_live_to_ogg_preserves_sample_rate_and_uses_mono_vorbis(
    tmp_path, monkeypatch
):
    source = tmp_path / "chunk.wav"
    output = tmp_path / "chunk.ogg"
    calls = []

    monkeypatch.setattr("backend.app.audio.shutil.which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output.write_bytes(b"ogg")

    monkeypatch.setattr("backend.app.audio.subprocess.run", fake_run)

    convert_live_to_ogg(source, output)

    assert calls == [
        (
            [
                "/usr/bin/ffmpeg",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-ac",
                "1",
                "-c:a",
                "libvorbis",
                "-q:a",
                str(LIVE_PROVIDER_VORBIS_QUALITY),
                str(output),
            ],
            {
                "check": True,
                "capture_output": True,
                "text": True,
                "timeout": LIVE_FFMPEG_TIMEOUT_SEC,
            },
        )
    ]


def test_convert_to_mp3_uses_44100_hz_mono_128_kbps(tmp_path, monkeypatch):
    source = tmp_path / "clip.wav"
    output = tmp_path / "clip.mp3"
    calls = []

    monkeypatch.setattr("backend.app.audio.shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "backend.app.audio.subprocess.run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    convert_to_mp3(source, output)

    assert calls == [
        (
            [
                "/usr/bin/ffmpeg",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-ar",
                "44100",
                "-ac",
                "1",
                "-b:a",
                "128k",
                str(output),
            ],
            {"check": True, "capture_output": True, "text": True, "timeout": 120},
        )
    ]
