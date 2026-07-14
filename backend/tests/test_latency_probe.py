from urllib.parse import parse_qs

import pytest

from scripts.live_chunk_latency_probe import (
    DirectCochlRunner,
    LocalApiRunner,
    ProbeRow,
    labels_from_raw_result,
    live_session_end_url,
    print_summary,
)


def test_live_session_end_url_preserves_api_prefix():
    assert live_session_end_url(
        "http://127.0.0.1:8000/demo/api/analyze-live-chunk?ignored=1"
    ) == "http://127.0.0.1:8000/demo/api/live-session/end"
    assert live_session_end_url(
        "http://127.0.0.1:8000/api/analyze-live-chunk/"
    ) == "http://127.0.0.1:8000/api/live-session/end"


def test_local_probe_finalizes_its_live_session(tmp_path, monkeypatch):
    wav_path = tmp_path / "chunk.wav"
    wav_path.write_bytes(b"wav")
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = parse_qs(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    runner = LocalApiRunner(
        wav_path,
        "http://127.0.0.1:8000/api/analyze-live-chunk",
        timeout=7,
    )

    runner.finish("probe-session")

    assert captured == {
        "url": "http://127.0.0.1:8000/api/live-session/end",
        "body": {"session_id": ["probe-session"]},
        "timeout": 7,
    }


def test_direct_probe_uses_backend_live_provider(tmp_path, monkeypatch):
    wav_path = tmp_path / "chunk.wav"
    wav_path.write_bytes(b"wav")
    captured = {}

    class FakeProvider:
        def __init__(self, settings):
            captured["timeout"] = settings.cochl_live_timeout_sec

        def analyze_live_chunk(self, audio_path):
            captured["audio_path"] = audio_path
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "classes": [
                                {"class": "Cough", "confidence": 0.95},
                            ]
                        }
                    ],
                }
            }

    monkeypatch.setenv("COCHL_PROJECT_KEY", "test-key")
    monkeypatch.setattr("scripts.live_chunk_latency_probe.CochlProvider", FakeProvider)
    runner = DirectCochlRunner(wav_path, timeout=7.5)

    row = runner.send(
        "probe-session",
        1,
        0.0,
        2.0,
        100.0,
        "scheduled",
        0,
    )

    assert row.status == "OK"
    assert row.detected_labels == "Cough 95%"
    assert captured == {"timeout": 7.5, "audio_path": wav_path}


def test_direct_probe_reads_both_documented_sound_event_keys():
    payload = {
        "status": "success",
        "results": [
            {
                "classes": [
                    {"class": "Silence", "confidence": 0.0},
                    {"class": "Knock", "confidence": 0.8},
                ]
            }
        ],
    }

    assert labels_from_raw_result({"sense": payload}) == ["Silence 0%", "Knock 80%"]
    assert labels_from_raw_result({"sound_event_detection": payload}) == [
        "Silence 0%",
        "Knock 80%",
    ]


def test_direct_probe_rejects_missing_or_malformed_service_payload():
    with pytest.raises(ValueError, match="missing"):
        labels_from_raw_result({})
    with pytest.raises(ValueError, match="invalid"):
        labels_from_raw_result({"sound_event_detection": {"results": {}}})


def test_probe_summary_excludes_warmup_and_reports_window_end_gate(capsys):
    common = {
        "mode": "local",
        "session_id": "probe-session",
        "status": "OK",
        "window_start_sec": 0.0,
        "window_end_sec": 2.0,
        "scheduled_at_iso": "scheduled",
    }
    rows = [
        ProbeRow(
            **common,
            sequence_id=1,
            phase="warmup",
            request_ms=9_000,
            window_end_delay_ms=9_000,
        ),
        ProbeRow(
            **common,
            sequence_id=2,
            phase="measurement",
            request_ms=750,
            window_end_delay_ms=760,
        ),
    ]

    print_summary(rows)

    output = capsys.readouterr().out
    assert "measured=1 warmup=1 ok=1 skip=0 error=0" in output
    assert "window_end_delay_ms: min=760ms p50=760ms p95=760ms max=760ms" in output
    assert "window_end_delay_ms >=2000ms: 0/1 (0.0%)" in output
    assert "9000ms" not in output
