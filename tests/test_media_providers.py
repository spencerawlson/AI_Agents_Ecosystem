"""ElevenLabs voiceover + HeyGen presenter: clients, costs, wiring."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from core import media_providers as mp

ENV = {"ELEVENLABS_API_KEY": "el-key", "ELEVENLABS_VOICE_ID": "voice 1",
       "HEYGEN_API_KEY": "hg-key", "HEYGEN_AVATAR_ID": "look-1",
       "ELEVENLABS_USD_PER_1K_CHARS": "0.30", "HEYGEN_USD_PER_MINUTE": "1.20"}


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Recorder:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, dict(req.header_items()),
                           json.loads(req.data) if req.data else None))
        return Resp(self.replies.pop(0))


def test_elevenlabs_voiceover_request_and_cost(tmp_path):
    rec = Recorder([b"ID3fake-mp3-bytes"])
    out = mp.elevenlabs_voiceover("Ship a  barrel\nto Haiti.", tmp_path / "v.mp3",
                                  env=ENV, urlopen=rec)
    method, url, headers, body = rec.calls[0]
    assert method == "POST"
    assert url == ("https://api.elevenlabs.io/v1/text-to-speech/voice%201"
                   "?output_format=mp3_44100_128")
    assert headers.get("Xi-api-key") == "el-key"
    assert body == {"text": "Ship a barrel to Haiti.", "model_id": "eleven_multilingual_v2"}
    assert (tmp_path / "v.mp3").read_bytes() == b"ID3fake-mp3-bytes"
    assert out["characters"] == 23 and out["cost_usd"] == pytest.approx(0.0069)


def test_heygen_v3_create_poll_download(tmp_path):
    rec = Recorder([
        json.dumps({"data": {"video_id": "vid-9", "status": "waiting"}}).encode(),
        json.dumps({"data": {"status": "processing"}}).encode(),
        json.dumps({"data": {"status": "completed", "duration": 30,
                             "video_url": "https://files.heygen.ai/vid-9.mp4"}}).encode(),
        b"MP4BYTES"])
    out = mp.heygen_presenter_video("Hello there", tmp_path / "p.mp4", env=ENV,
                                    urlopen=rec, title="Intro", sleep=lambda s: None)
    create = rec.calls[0]
    assert create[0] == "POST" and create[1] == "https://api.heygen.com/v3/videos"
    assert create[2].get("X-api-key") == "hg-key"
    assert create[3] == {"type": "avatar", "avatar_id": "look-1", "script": "Hello there",
                         "aspect_ratio": "9:16", "resolution": "720p", "title": "Intro"}
    assert rec.calls[1][1] == "https://api.heygen.com/v3/videos/vid-9"
    assert (tmp_path / "p.mp4").read_bytes() == b"MP4BYTES"
    assert out["seconds"] == 30 and out["cost_usd"] == pytest.approx(0.6)


def test_heygen_failure_is_reported(tmp_path):
    rec = Recorder([json.dumps({"data": {"video_id": "v"}}).encode(),
                    json.dumps({"data": {"status": "failed",
                                         "failure_message": "avatar not found"}}).encode()])
    with pytest.raises(mp.MediaProviderError, match="avatar not found"):
        mp.heygen_presenter_video("Hi", tmp_path / "p.mp4", env=ENV, urlopen=rec,
                                  sleep=lambda s: None)


def test_no_keys_means_clear_blockers_and_no_calls(tmp_path):
    assert mp.voice_blocker({}) == "needs ELEVENLABS_API_KEY on the server"
    assert mp.presenter_blocker({"HEYGEN_API_KEY": "k"}) == "needs HEYGEN_AVATAR_ID on the server"
    with pytest.raises(mp.MediaProviderError, match="Voiceover unavailable"):
        mp.elevenlabs_voiceover("hi", tmp_path / "v.mp3", env={}, urlopen=Recorder([]))


def test_cost_needs_the_owners_rate():
    assert mp.voice_cost_usd(1000, env={}) is None
    assert mp.presenter_cost_usd(90, env={"HEYGEN_USD_PER_MINUTE": "2"}) == 3.0


def test_scenes_follow_the_voiceover_length():
    script = {"scenes": [{"narration": "a" * 10}, {"narration": "b" * 30}]}
    mp.fit_scenes_to_audio(script, 19.5)
    d = [s["duration_s"] for s in script["scenes"]]
    assert d == [5.0, 15.0]  # 20s split 1:3, within the 3-15s limits


def test_media_usage_counts_in_site_costs():
    from ventoryn.saas import SaasStore

    store = SaasStore(":memory:")
    acct = store.create_account("s", "a@example.com", "A")
    site = store.create_site(acct["id"], "https://shop.example.com")
    store.record_media_usage(acct["id"], site["id"], "elevenlabs", 1200, "characters", 0.36)
    store.record_media_usage(acct["id"], site["id"], "heygen", 30, "seconds", None)  # rate unset
    assert store.media_spend_usd(site["id"]) == pytest.approx(0.36)
    # Media spend never counts toward the social posting caps.
    assert store.social_spend_in_period("", site_id=site["id"]) == 0


def test_job_form_shows_options_or_what_is_missing(monkeypatch):
    from fastapi.testclient import TestClient

    from _signin import operator_store_and_config, sign_in
    from ventoryn.app import create_app

    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    store, config = operator_store_and_config()
    client = TestClient(create_app(saas_store=store, saas_config=config))
    acct = sign_in(client, store, email="ada@example.com", sub="s-a", name="Ada")
    site = store.create_site(acct["id"], "https://shop.example.com")
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "Voiceover (ElevenLabs): needs ELEVENLABS_API_KEY on the server." in html
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "name='voiceover' value='on' checked" in html


def _video_ctx(tmp_path, store):
    from datetime import datetime, timezone
    return {"store": store, "reports_dir": tmp_path, "date": "2026-11-11",
            "stamp": "20261111-120000", "now": datetime.now(timezone.utc)}


def _script():
    return {"title": "Intro", "scenes": [
        {"on_screen_text": f"Scene {i}", "narration": "Words " * 8, "duration_s": 5}
        for i in range(3)]}


def test_video_job_uses_voiceover_and_records_usage(tmp_path, monkeypatch):
    from ventoryn import site_jobs
    from ventoryn.saas import SaasStore
    import core.video_render as vr

    store = SaasStore(":memory:")
    acct = store.create_account("s", "a@example.com", "A")
    site = store.create_site(acct["id"], "https://shop.example.com")
    monkeypatch.setattr(mp, "voice_ready", lambda env=None: True)
    monkeypatch.setattr(mp, "elevenlabs_voiceover", lambda text, out, **k: (
        Path(out).write_bytes(b"ID3"), {"path": str(out), "characters": len(text), "cost_usd": 0.05})[1])
    monkeypatch.setattr(mp, "audio_seconds", lambda p: 21.0)
    seen = {}
    monkeypatch.setattr(vr, "render_video", lambda script, out, size, audio=None: seen.update(
        audio=audio, total=sum(s["duration_s"] for s in script["scenes"])) or {
        "video": str(out), "srt": "x.srt", "duration_s": 21.5, "scenes": 3})
    rendered, extras = site_jobs._make_video(site, _script(), tmp_path, "stem", {"voiceover": "on"},
                                             _video_ctx(tmp_path, store))
    assert seen["audio"].name == "stem.mp3" and seen["total"] == pytest.approx(21.5, abs=0.05)
    assert "Voiceover: ElevenLabs" in extras[0] and "$0.05" in extras[0]
    assert store.media_spend_usd(site["id"]) == pytest.approx(0.05)


def test_video_job_presenter_and_voiceover_off(tmp_path, monkeypatch):
    from ventoryn import site_jobs
    from ventoryn.saas import SaasStore
    import core.video_render as vr

    store = SaasStore(":memory:")
    acct = store.create_account("s", "a@example.com", "A")
    site = store.create_site(acct["id"], "https://shop.example.com")
    monkeypatch.setattr(mp, "heygen_presenter_video", lambda script, out, **k: {
        "path": str(out), "video_id": "v", "seconds": 40.0, "cost_usd": None})
    rendered, extras = site_jobs._make_video(site, _script(), tmp_path, "stem", {"presenter": "heygen"},
                                             _video_ctx(tmp_path, store))
    assert "HeyGen" in extras[0] and "price not set" in extras[0]
    called = {}
    monkeypatch.setattr(mp, "voice_ready", lambda env=None: True)
    monkeypatch.setattr(mp, "elevenlabs_voiceover", lambda *a, **k: called.setdefault("voice", True))
    monkeypatch.setattr(vr, "render_video", lambda script, out, size, audio=None: {
        "video": str(out), "srt": "", "duration_s": 15, "scenes": 3, "audio": audio})
    site_jobs._make_video(site, _script(), tmp_path, "stem2", {"voiceover": "off"},
                          _video_ctx(tmp_path, store))
    assert "voice" not in called  # the owner turned the voiceover off
