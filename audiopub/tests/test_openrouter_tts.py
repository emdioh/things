import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from audiopub import casting
from audiopub.cli import main
from audiopub.tts import BACKENDS, make_tts
from audiopub.tts.base import Voice


def _mp3(seconds=1) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=f=300:d={seconds}", "-f", "mp3", "-"],
        check=True, capture_output=True).stdout


def _duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], check=True, capture_output=True, text=True).stdout
    return float(out.strip())


class Server:
    def __init__(self):
        self.requests = []   # dicts: path, auth, body
        self.fail_voices = set()
        audio = _mp3()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                if body.get("voice") in outer.fail_voices:
                    data = b'{"error": {"message": "bad voice"}}'
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                else:
                    data = audio
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/api/v1"
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = Server()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("AUDIOPUB_TTS_VOICES", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_openrouter_request(server, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    tts = make_tts("openrouter", base_url=server.url)
    tts.render("Hello", "en_paul_neutral", 1.0, tmp_path / "a.mp3")
    (req,) = server.requests
    assert req["path"] == "/api/v1/audio/speech"
    assert req["auth"] == "Bearer or-key"
    assert set(req["body"]) == {"model", "input", "voice", "response_format"}
    assert req["body"] == {"model": "mistralai/voxtral-mini-tts-3-0", "input": "Hello",
                           "voice": "en_paul_neutral", "response_format": "mp3"}
    assert (tmp_path / "a.mp3").stat().st_size > 0


def test_openrouter_model_override(server, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    tts = make_tts("openrouter", model="mistralai/other", base_url=server.url)
    tts.render("Hi", "v", 1.0, tmp_path / "a.mp3")
    assert server.requests[0]["body"]["model"] == "mistralai/other"


def test_openrouter_api_key_arg(server, tmp_path):
    tts = make_tts("openrouter", api_key="explicit", base_url=server.url)
    tts.render("Hi", "v", 1.0, tmp_path / "a.mp3")
    assert server.requests[0]["auth"] == "Bearer explicit"


@pytest.mark.parametrize("speed", [1.0, 0.8])
def test_openrouter_speed_is_applied_with_ffmpeg(server, monkeypatch, tmp_path, speed):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    ref = tmp_path / "ref.mp3"
    ref.write_bytes(_mp3())
    original = _duration(ref)
    tts = make_tts("openrouter", base_url=server.url)
    out = tmp_path / "out.mp3"
    tts.render("Hello", "voice_3150", speed, out)
    assert "speed" not in server.requests[0]["body"]
    assert _duration(out) == pytest.approx(original / speed, abs=0.1)
    assert not list(tmp_path.glob("*.tempo*"))


def test_openai_sends_speed_and_key(server, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "oa-key")
    tts = make_tts("openai", base_url=server.url)
    tts.render("Hello", "alloy", 1.25, tmp_path / "a.mp3")
    (req,) = server.requests
    assert req["path"] == "/api/v1/audio/speech"
    assert req["auth"] == "Bearer oa-key"
    assert req["body"]["speed"] == 1.25
    assert req["body"]["model"] == "gpt-4o-mini-tts"


def test_backends_include_openrouter():
    assert "openrouter" in BACKENDS


def test_default_catalog(monkeypatch):
    tts = make_tts("openrouter")
    voices = {v.id: v for v in tts.catalog()}
    assert voices["en_paul_neutral"] == Voice("en_paul_neutral", "male", "en-US")
    assert voices["gb_oliver_neutral"] == Voice("gb_oliver_neutral", "male", "en-GB")
    assert voices["gb_jane_neutral"] == Voice("gb_jane_neutral", "female", "en-GB")
    assert voices["fr_marie_neutral"] == Voice("fr_marie_neutral", "female", "fr-FR")
    assert voices["voice_3150"] == Voice("voice_3150", "unknown", "*")


def test_catalog_2603_has_no_numbered_voices():
    tts = make_tts("openrouter", model="mistralai/voxtral-mini-tts-2603")
    ids = [v.id for v in tts.catalog()]
    assert len(ids) == 4
    assert not any(i.startswith("voice_") for i in ids)


def test_voices_language_first():
    voices = make_tts("openrouter").voices("fr")
    assert voices[0].id == "fr_marie_neutral"
    assert len(voices) == len(make_tts("openrouter").catalog())


SPEC = "voice_3150:male:it-IT,voice_478:female:it-IT,x:bogus"


def test_custom_voices_parsing(monkeypatch):
    from audiopub.tts.openai_tts import custom_voices

    assert custom_voices() is None
    monkeypatch.setenv("AUDIOPUB_TTS_VOICES", SPEC)
    assert custom_voices() == [Voice("voice_3150", "male", "it-IT"), Voice("voice_478", "female", "it-IT"),
                               Voice("x", "unknown", "*")]


def test_custom_voices_replace_catalog_and_cast(monkeypatch):
    monkeypatch.setenv("AUDIOPUB_TTS_VOICES", SPEC)
    tts = make_tts("openrouter")
    voices = tts.voices("it")
    assert {v.id for v in voices} == {"voice_3150", "voice_478", "x"}
    assert [v.id for v in voices[:2]] == ["voice_3150", "voice_478"]
    assert voices[2].id == "x"
    picked = casting.pick_voices(voices, "it")
    assert [v.id for v in picked] == ["voice_3150", "voice_478"]
    assert all(v.locale == "it-IT" for v in picked)


def test_no_api_key_does_not_crash():
    tts = make_tts("openrouter")
    assert tts.name == "openrouter"
    assert tts.model == "mistralai/voxtral-mini-tts-3-0"


def test_voices_sample_continues_after_failure(server, monkeypatch, tmp_path, capsys):
    import time

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("AUDIOPUB_TTS_VOICES", "good1:male:it-IT,bad:female:it-IT,good2:female:it-IT")
    server.fail_voices = {"bad"}
    d = tmp_path / "samples"
    argv = ["voices", "--tts", "openrouter", "--lang", "it", "--sample", "Ciao",
            "--sample-dir", str(d), "--tts-base-url", server.url]
    t0 = time.time()
    main(argv)
    assert time.time() - t0 < 10
    out = capsys.readouterr().out
    assert "✓ good1.mp3" in out and "✓ good2.mp3" in out
    assert "✗ bad" in out
    assert (d / "good1.mp3").exists() and (d / "good2.mp3").exists()
    assert not (d / "bad.mp3").exists()
    assert not list(d.glob("*.part*"))
    assert all(r["body"]["input"] == "Ciao" for r in server.requests)

    # Second run only retries the missing voice.
    server.requests.clear()
    main(argv)
    assert {r["body"]["voice"] for r in server.requests} == {"bad"}
    assert "Recording 1 sample(s)" in capsys.readouterr().out


def test_voices_sample_default_dir(server, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUDIOPUB_TTS_VOICES", "a:male:it-IT")
    main(["voices", "--tts", "openrouter", "--lang", "it", "--sample", "Ciao", "--tts-base-url", server.url])
    assert (tmp_path / "voice-samples" / "openrouter" / "a.mp3").exists()
