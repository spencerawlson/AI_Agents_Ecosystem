"""Video: script validation, slides, SRT, ffmpeg render, the creative
agent's video-script type and the video mission/CLI.

No network and no real LLM anywhere — the agent runs against a fake
gateway. The render tests use the real ffmpeg binary when present
(skip otherwise); nothing is posted.
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.video_render import (
    SIZES, load_script, render_slides, render_video, validate_script,
    write_srt,
)
import ecosystem.road2cissp_growth as growth
from ecosystem.road2cissp_growth import load_state, save_state

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


def _script(**over):
    script = {
        "title": "CISSP in 60 seconds",
        "scenes": [
            {"on_screen_text": "Domain 3 is about models.",
             "narration": "Domain three is where the security models live.",
             "duration_s": 5},
            {"on_screen_text": "Models answer: who can do what?",
             "narration": "Every model answers who can do what, and how.",
             "duration_s": 5},
            {"on_screen_text": "Practice with real questions.",
             "narration": "Practice with real questions until it clicks.",
             "duration_s": 5},
        ],
    }
    script.update(over)
    return script


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


# -- validation -------------------------------------------------------------

def test_validate_script_valid_and_each_rule():
    assert validate_script(_script()) == []
    assert any("min 3" in e for e in
               validate_script(_script(scenes=_script()["scenes"][:2])))
    assert any("max 8" in e for e in
               validate_script(_script(
                   scenes=[_script()["scenes"][0]] * 9)))
    bad_text = _script()
    bad_text["scenes"][0]["on_screen_text"] = "x" * 141
    assert any("max 140" in e for e in validate_script(bad_text))
    bad_narr = _script()
    bad_narr["scenes"][1]["narration"] = ""
    assert any("missing narration" in e for e in validate_script(bad_narr))
    bad_dur = _script()
    bad_dur["scenes"][0]["duration_s"] = 2
    assert any("3–15s" in e for e in validate_script(bad_dur))
    bad_total = _script()
    for s in bad_total["scenes"]:
        s["duration_s"] = 3
    assert any("min 15s" in e for e in validate_script(bad_total))
    assert any("missing title" in e for e in
               validate_script(_script(title="")))
    assert validate_script("nope") == ["script must be a JSON object"]


def test_load_script_rejects_bad_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot read script"):
        load_script(path)


# -- slides and subtitles -----------------------------------------------------

def test_render_slides_size_and_count(tmp_path):
    paths = render_slides(_script(), tmp_path / "slides",
                          size=SIZES["vertical"])
    assert len(paths) == 3
    from PIL import Image

    for p in paths:
        with Image.open(p) as img:
            assert img.size == (1080, 1920)
            assert img.mode == "RGB"


def test_write_srt_timings(tmp_path):
    path = write_srt(_script(), tmp_path / "out.srt")
    text = path.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:05,000" in text
    assert "00:00:05,000 --> 00:00:10,000" in text
    assert "00:00:10,000 --> 00:00:15,000" in text
    assert "Domain three is where the security models live." in text


# -- ffmpeg render ------------------------------------------------------------

@needs_ffmpeg
def test_render_video_real_mp4(tmp_path):
    out = tmp_path / "video.mp4"
    result = render_video(_script(), out, size=(540, 960))
    assert result["duration_s"] == 15
    assert result["scenes"] == 3
    assert Path(result["srt"]).exists()
    info = _probe(out)
    assert abs(float(info["format"]["duration"]) - 15) < 0.6
    video = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    assert (video["width"], video["height"]) == (540, 960)
    assert video["codec_name"] == "h264"


@needs_ffmpeg
def test_render_video_with_short_voiceover_keeps_full_length(tmp_path):
    audio = tmp_path / "voice.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=3", "-c:a", "libmp3lame",
         str(audio)], check=True)
    out = tmp_path / "video.mp4"
    result = render_video(_script(), out, size=(540, 960), audio=audio)
    assert result["audio"] is True
    info = _probe(out)
    # The 3s voiceover must not cut the 15s video.
    assert abs(float(info["format"]["duration"]) - 15) < 0.6
    assert any(s["codec_type"] == "audio" for s in info["streams"])


def test_render_video_rejects_invalid_script(tmp_path):
    with pytest.raises(ValueError, match="invalid video script"):
        render_video(_script(scenes=[]), tmp_path / "v.mp4")


def test_render_video_missing_ffmpeg(tmp_path):
    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        render_video(_script(), tmp_path / "v.mp4",
                     ffmpeg="definitely-not-a-binary")


# -- creative agent -------------------------------------------------------------

def _fake_gateway(payload):
    cls = MagicMock()
    cls.enabled.return_value = True
    cls.SMART = "smart"
    inst = MagicMock()
    inst.complete.return_value = {
        "text": "{}", "json": payload, "model": "fake",
        "input_tokens": 100, "output_tokens": 200, "cost_usd": 0.01}
    cls.return_value = inst
    return cls


def test_content_pieces_writes_video_script():
    from agents.creative.agent import CreativeAgent
    from orchestrator.models import Task

    piece = {"type": "video-script", "title": "CISSP in 60 seconds",
             "scenes": _script()["scenes"]}
    task = Task(agent_type="creative", business_id="biz_1",
                inputs={"action": "content_pieces",
                        "experiment": {"slug": "reddit-answers",
                                       "content_needed": ["video-script"]},
                        "profile": {"site_url": "https://road2cissp.com"}},
                budget_usd=5.0, budget_tokens=50000)
    with patch("core.llm.LLMGateway",
               _fake_gateway({"pieces": [piece]})):
        agent = CreativeAgent()
        out = agent(task)
    assert len(out["pieces"]) == 1
    assert out["pieces"][0]["type"] == "video-script"
    assert out["pieces"][0]["asset_id"]
    # The asset body is the script JSON, so the renderer can read it back.
    asset = agent._assets[out["pieces"][0]["asset_id"]]
    assert "on_screen_text" in asset.content
    assert json.loads(asset.content)["scenes"][0]["duration_s"] == 5


# -- mission and CLI ------------------------------------------------------------

def _state(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, {"last_run": {}, "last_attempt": {}, "metrics": [],
                      "experiments": {}, "strategy_history": [],
                      "spend_usd": 0.0})
    return path


@needs_ffmpeg
def test_run_video_from_script_file(tmp_path, monkeypatch):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    _state(tmp_path)
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(_script()), encoding="utf-8")
    from ecosystem.runtime import build_runtime

    rt = build_runtime(use_llm=False)
    summary = growth.run_video(
        rt, state_path=tmp_path / "state.json",
        reports_root=tmp_path / "reports", now=NOW,
        script_path=script_path)
    assert not summary["errors"], summary["errors"]
    assert summary["spend_usd"] == 0
    video = summary["video"]
    assert video["duration_s"] == 15
    assert Path(video["video_path"]).exists()
    assert Path(video["srt_path"]).exists()
    info = _probe(video["video_path"])
    assert abs(float(info["format"]["duration"]) - 15) < 0.6
    assert Path(summary["report"]).exists()
    assert "NOT YET POSTED" in Path(summary["report"]).read_text(encoding="utf-8")
    saved = load_state(tmp_path / "state.json")
    assert saved["videos"]["cissp-in-60-seconds"]["status"] == "rendered"


def test_run_video_rejects_invalid_script(tmp_path, monkeypatch):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    _state(tmp_path)
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(_script(scenes=[])), encoding="utf-8")
    from ecosystem.runtime import build_runtime

    rt = build_runtime(use_llm=False)
    summary = growth.run_video(
        rt, state_path=tmp_path / "state.json",
        reports_root=tmp_path / "reports", now=NOW,
        script_path=script_path)
    assert summary["video"] is None
    assert any("invalid video script" in e for e in summary["errors"])


@needs_ffmpeg
def test_run_video_from_experiment_drafts_then_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    path = _state(tmp_path)
    state = load_state(path)
    state["experiments"] = {
        "reddit-answers": {"slug": "reddit-answers",
                           "name": "Reddit answers", "status": "active",
                           "channel": "community",
                           "hypothesis": "helpful answers drive visits",
                           "content_needed": ["community"]},
    }
    save_state(path, state)
    piece = {"type": "video-script", "title": "CISSP in 60 seconds",
             "scenes": _script()["scenes"]}
    from ecosystem.runtime import build_runtime

    rt = build_runtime(use_llm=True)
    with patch("core.llm.LLMGateway",
               _fake_gateway({"pieces": [piece]})):
        summary = growth.run_video(
            rt, state_path=path, reports_root=tmp_path / "reports",
            now=NOW, experiment_slug="reddit-answers")
    assert not summary["errors"], summary["errors"]
    assert summary["spend_usd"] == pytest.approx(0.01)
    assert summary["video"]["duration_s"] == 15
    assert Path(summary["video"]["video_path"]).exists()
    saved = load_state(path)
    assert "reddit-answers" in saved["videos"]


def test_cli_video_needs_script_or_experiment(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    _state(tmp_path)
    assert growth.main(["video"]) == 1
    assert "need --script" in capsys.readouterr().out
