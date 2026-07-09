from urllib.parse import parse_qs

from scripts.live_chunk_latency_probe import LocalApiRunner, live_session_end_url


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
