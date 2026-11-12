"""Voice and presenter video for agent-made videos: ElevenLabs + HeyGen.

Both are paid external APIs, called only when their keys are set on the
server (never in the repo):

    ELEVENLABS_API_KEY      enables voiceovers
    ELEVENLABS_VOICE_ID     the voice to use (list: GET /v2/voices)
    ELEVENLABS_MODEL_ID     optional, default eleven_multilingual_v2
    HEYGEN_API_KEY          enables a talking presenter
    HEYGEN_AVATAR_ID        the avatar look (list: GET /v3/avatars/looks)
    HEYGEN_VOICE_ID         optional: the avatar's default voice otherwise

Prices are per plan and not published in either API's docs, so usage is
recorded in the unit each provider bills (characters, video seconds).
A dollar figure is shown only when the owner sets the rate of their own
plan:

    ELEVENLABS_USD_PER_1K_CHARS   e.g. 0.30
    HEYGEN_USD_PER_MINUTE         e.g. 1.00

API shapes checked against the official docs on 2026-11-11:
- ElevenLabs: POST /v1/text-to-speech/{voice_id}, header xi-api-key,
  body {text, model_id}, query output_format; returns raw audio bytes.
- HeyGen v3 (v1/v2 were sunset 2026-10-31): POST /v3/videos with
  {type: "avatar", avatar_id, script, voice_id?, aspect_ratio, resolution},
  header x-api-key -> data.video_id; GET /v3/videos/{id} -> data.status
  (pending | processing | completed | failed), data.video_url (presigned).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ELEVENLABS_BASE = "https://api.elevenlabs.io"
HEYGEN_BASE = "https://api.heygen.com"
DEFAULT_MODEL = "eleven_multilingual_v2"


class MediaProviderError(RuntimeError):
    """A provider refused or failed; the message is safe to show (no key)."""


def _env(name: str, env=None) -> str:
    return ((env if env is not None else os.environ).get(name) or "").strip()


def _rate(name: str, env=None) -> float | None:
    try:
        v = float(_env(name, env))
        return v if v >= 0 else None
    except ValueError:
        return None


# -- what is available ------------------------------------------------------

def voice_ready(env=None) -> bool:
    return bool(_env("ELEVENLABS_API_KEY", env) and _env("ELEVENLABS_VOICE_ID", env))


def presenter_ready(env=None) -> bool:
    return bool(_env("HEYGEN_API_KEY", env) and _env("HEYGEN_AVATAR_ID", env))


def voice_blocker(env=None) -> str | None:
    if not _env("ELEVENLABS_API_KEY", env):
        return "needs ELEVENLABS_API_KEY on the server"
    if not _env("ELEVENLABS_VOICE_ID", env):
        return "needs ELEVENLABS_VOICE_ID on the server"
    return None


def presenter_blocker(env=None) -> str | None:
    if not _env("HEYGEN_API_KEY", env):
        return "needs HEYGEN_API_KEY on the server"
    if not _env("HEYGEN_AVATAR_ID", env):
        return "needs HEYGEN_AVATAR_ID on the server"
    return None


# -- costs ------------------------------------------------------------------

def voice_cost_usd(characters: int, env=None) -> float | None:
    rate = _rate("ELEVENLABS_USD_PER_1K_CHARS", env)
    return None if rate is None else round(characters / 1000 * rate, 4)


def presenter_cost_usd(seconds: float, env=None) -> float | None:
    rate = _rate("HEYGEN_USD_PER_MINUTE", env)
    return None if rate is None else round(seconds / 60 * rate, 4)


# -- HTTP -------------------------------------------------------------------

def _request(url: str, *, method: str = "GET", headers: dict, body: dict | None = None,
             urlopen=None, timeout: int = 120) -> tuple[int, bytes]:
    opener = urlopen or urllib.request.urlopen
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**headers, **({"Content-Type": "application/json"}
                                                        if body is not None else {})})
    try:
        with opener(req, timeout=timeout) as resp:
            return getattr(resp, "status", 200), resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise MediaProviderError(f"HTTP {exc.code} from {urllib.parse.urlparse(url).netloc}"
                                 + (f": {detail}" if detail else "")) from None
    except urllib.error.URLError as exc:
        raise MediaProviderError(f"could not reach {urllib.parse.urlparse(url).netloc} "
                                 f"({exc.reason})") from None


# -- ElevenLabs: text -> MP3 ------------------------------------------------

def elevenlabs_voiceover(text: str, out_path: Path, *, env=None, urlopen=None,
                         voice_id: str = "") -> dict:
    """Write an MP3 voiceover of ``text`` (``voice_id`` overrides the
    server's default voice). Returns {path, characters, cost_usd}."""
    if not _env("ELEVENLABS_API_KEY", env):
        raise MediaProviderError("Voiceover unavailable — needs ELEVENLABS_API_KEY on the server")
    if not (voice_id or _env("ELEVENLABS_VOICE_ID", env)):
        raise MediaProviderError("Voiceover unavailable — no voice chosen")
    text = " ".join(str(text).split())
    if not text:
        raise MediaProviderError("nothing to say: the script has no narration")
    voice = urllib.parse.quote(voice_id or _env("ELEVENLABS_VOICE_ID", env), safe="")
    url = f"{ELEVENLABS_BASE}/v1/text-to-speech/{voice}?output_format=mp3_44100_128"
    status, audio = _request(url, method="POST",
                             headers={"xi-api-key": _env("ELEVENLABS_API_KEY", env),
                                      "Accept": "audio/mpeg"},
                             body={"text": text,
                                   "model_id": _env("ELEVENLABS_MODEL_ID", env) or DEFAULT_MODEL},
                             urlopen=urlopen)
    if not audio or audio[:1] == b"{":
        raise MediaProviderError("ElevenLabs returned no audio")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(audio)
    return {"path": str(out_path), "characters": len(text),
            "cost_usd": voice_cost_usd(len(text), env)}


# -- HeyGen v3: script -> presenter MP4 -------------------------------------

def heygen_create(script: str, *, env=None, urlopen=None, aspect_ratio: str = "9:16",
                  resolution: str = "720p", title: str = "", avatar_id: str = "") -> str:
    if not _env("HEYGEN_API_KEY", env):
        raise MediaProviderError("Presenter unavailable — needs HEYGEN_API_KEY on the server")
    avatar = avatar_id or _env("HEYGEN_AVATAR_ID", env)
    if not avatar or avatar == "none":
        raise MediaProviderError("Presenter unavailable — no presenter chosen for this website")
    body = {"type": "avatar", "avatar_id": avatar,
            "script": " ".join(str(script).split()), "aspect_ratio": aspect_ratio,
            "resolution": resolution}
    # The avatar's own voice unless the server sets one — and only for
    # the server's default avatar (another avatar keeps its own voice).
    if _env("HEYGEN_VOICE_ID", env) and not avatar_id:
        body["voice_id"] = _env("HEYGEN_VOICE_ID", env)
    if title:
        body["title"] = title[:100]
    _status, raw = _request(f"{HEYGEN_BASE}/v3/videos", method="POST",
                            headers={"x-api-key": _env("HEYGEN_API_KEY", env)},
                            body=body, urlopen=urlopen)
    video_id = ((json.loads(raw or b"{}").get("data") or {}).get("video_id") or "")
    if not video_id:
        raise MediaProviderError("HeyGen did not return a video id")
    return video_id


def heygen_status(video_id: str, *, env=None, urlopen=None) -> dict:
    vid = urllib.parse.quote(video_id, safe="")
    _status, raw = _request(f"{HEYGEN_BASE}/v3/videos/{vid}",
                            headers={"x-api-key": _env("HEYGEN_API_KEY", env)},
                            urlopen=urlopen)
    return json.loads(raw or b"{}").get("data") or {}


def heygen_presenter_video(script: str, out_path: Path, *, env=None, urlopen=None,
                           title: str = "", timeout_s: int = 900, poll_s: float = 10,
                           sleep=time.sleep, avatar_id: str = "") -> dict:
    """Make a vertical presenter video and download it.
    Returns {path, video_id, seconds, cost_usd}."""
    video_id = heygen_create(script, env=env, urlopen=urlopen, title=title,
                             avatar_id=avatar_id)
    waited = 0.0
    while True:
        data = heygen_status(video_id, env=env, urlopen=urlopen)
        status = data.get("status")
        if status == "completed" and data.get("video_url"):
            break
        if status == "failed":
            raise MediaProviderError("HeyGen could not make the video: "
                                     + str(data.get("failure_message") or data.get("failure_code")
                                           or "no reason given"))
        if waited >= timeout_s:
            raise MediaProviderError(f"HeyGen video {video_id} not ready after "
                                     f"{timeout_s // 60} minutes")
        sleep(poll_s)
        waited += poll_s
    url = data["video_url"]
    if not url.startswith("https://"):
        raise MediaProviderError("HeyGen returned an unexpected video address")
    _s, video = _request(url, headers={}, urlopen=urlopen, timeout=300)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(video)
    seconds = float(data.get("duration") or 0)
    return {"path": str(out_path), "video_id": video_id, "seconds": seconds,
            "cost_usd": presenter_cost_usd(seconds, env)}


# -- choices for the settings form (cached: one listing per hour) ------------

_LIST_CACHE: dict = {}


def _cached(name, fetch, ttl=3600):
    hit = _LIST_CACHE.get(name)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        value = fetch()
    except Exception:  # noqa: BLE001 - a listing failure only empties the menu
        value = []
    _LIST_CACHE[name] = (time.time(), value)
    return value


def list_voices(env=None, urlopen=None) -> list[dict]:
    """[{id, name}] of the ElevenLabs voices on the account ([] without a key)."""
    if not _env("ELEVENLABS_API_KEY", env):
        return []

    def fetch():
        _s, raw = _request(f"{ELEVENLABS_BASE}/v2/voices?page_size=100",
                           headers={"xi-api-key": _env("ELEVENLABS_API_KEY", env)},
                           urlopen=urlopen, timeout=20)
        return [{"id": v["voice_id"], "name": v.get("name", "")}
                for v in json.loads(raw).get("voices", [])]
    return _cached("voices", fetch)


def list_avatars(env=None, urlopen=None) -> list[dict]:
    """[{id, name, private}] HeyGen avatar looks: the account's own first,
    then up to 50 public ones ([] without a key)."""
    if not _env("HEYGEN_API_KEY", env):
        return []

    def fetch():
        out = []
        for own in ("private", "public"):
            _s, raw = _request(f"{HEYGEN_BASE}/v3/avatars/looks?ownership={own}&limit=50",
                               headers={"x-api-key": _env("HEYGEN_API_KEY", env)},
                               urlopen=urlopen, timeout=20)
            out += [{"id": a["id"], "name": a.get("name", ""), "private": own == "private"}
                    for a in json.loads(raw).get("data", [])]
        return out
    return _cached("avatars", fetch)


# -- fitting scenes to a voiceover -------------------------------------------

def fit_scenes_to_audio(script: dict, audio_seconds: float,
                        min_s: float = 3, max_s: float = 15) -> dict:
    """Re-time scenes so the slides last as long as the voiceover: each
    scene gets a share proportional to its narration length, within the
    renderer's per-scene limits."""
    scenes = script.get("scenes") or []
    if not scenes or audio_seconds <= 0:
        return script
    target = audio_seconds + 0.5
    weights = [max(1, len(str(s.get("narration") or ""))) for s in scenes]
    total = sum(weights)
    for s, w in zip(scenes, weights):
        s["duration_s"] = round(min(max_s, max(min_s, target * w / total)), 2)
    return script


def audio_seconds(path: Path, ffprobe: str = "ffprobe") -> float:
    """Length of an audio file, via ffprobe (0 when unknown)."""
    import shutil
    import subprocess

    if shutil.which(ffprobe) is None:
        return 0.0
    proc = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=nw=1:nk=1", str(path)],
                          capture_output=True, text=True)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0
