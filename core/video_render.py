"""Video rendering: turn a video script into a real MP4 slideshow.

HeyCatch generates up to 20 AI videos/slideshows a day. The
ecosystem could only *name* a video: the content calendar can schedule
a "video-script" day, but nothing wrote the script and nothing
rendered it. This module closes both halves, with the creative agent
writing the script (``content_pieces`` type ``video-script``) and
this renderer turning it into a file.

A script is the contract both sides share::

    {"title": "CISSP in 60 seconds",
     "scenes": [{"on_screen_text": "Domain 3 is about models.",
                 "narration": "Domain three is where the security models live...",
                 "duration_s": 6}, ...]}

Rules (enforced by :func:`validate_script`, same gate the writer and
the mission both use): 3–8 scenes, each with on-screen text (max 140
characters — it has to fit on a phone screen) and narration, each
3–15 seconds, total 15–90 seconds.

What the render produces, for each script:

- **Slides** (Pillow): 1080x1920 vertical by default — TikTok /
  Reels / Shorts — or 1920x1080 wide for YouTube. Light design, the
  Road to CISSP direction: white, near-black text, a violet accent
  bar, the site name as a footer. No stock photos, no generated
  imagery — the words are the video, which is honest for a study
  product and keeps the render deterministic.
- **An MP4** (ffmpeg, libx264): one slide per scene at its scripted
  duration, fade in/out, 30 fps. Optional voiceover: pass an audio
  file (the owner's own recording — Spencer records his narration
  himself — or any mp3/wav) and it is muxed in; without one the
  video is silent and the SRT carries the words.
- **An SRT file** from the narration, timed to the scenes, so the
  video is watchable muted — which is how most social video is
  watched.

What it does NOT do: no AI voice (no TTS key in the repo — a
voiceover file is the input, not a generated asset), no stock or
AI-generated footage, no auto-posting. A rendered video is a file
under ``reports/videos/``; the owner posts it by hand. The render
needs ffmpeg on PATH and Pillow installed (``requirements.txt``);
both are checked up front with a plain-language error, never a
silent fallback to a fake video.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

SIZES = {
    "vertical": (1080, 1920),
    "wide": (1920, 1080),
}

MIN_SCENES = 3
MAX_SCENES = 8
MIN_SCENE_S = 3
MAX_SCENE_S = 15
MIN_TOTAL_S = 15
MAX_TOTAL_S = 90
MAX_ON_SCREEN = 140

# Road to CISSP light direction.
BG = (255, 255, 255)
INK = (17, 17, 17)
MUTED = (90, 90, 90)
VIOLET = (196, 181, 253)
VIOLET_DEEP = (109, 40, 217)
SITE = "road2cissp.com"

_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def validate_script(script: dict) -> list[str]:
    """Deterministic gate on a video script. Empty list = valid."""
    errors: list[str] = []
    if not isinstance(script, dict):
        return ["script must be a JSON object"]
    if not (script.get("title") or "").strip():
        errors.append("missing title")
    scenes = script.get("scenes")
    if not isinstance(scenes, list):
        return errors + ["scenes must be a list"]
    if len(scenes) < MIN_SCENES:
        errors.append(f"only {len(scenes)} scenes (min {MIN_SCENES})")
    if len(scenes) > MAX_SCENES:
        errors.append(f"{len(scenes)} scenes (max {MAX_SCENES})")
    total = 0.0
    for i, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            errors.append(f"scene {i}: not an object")
            continue
        text = scene.get("on_screen_text") or ""
        if not text.strip():
            errors.append(f"scene {i}: missing on_screen_text")
        elif len(text) > MAX_ON_SCREEN:
            errors.append(f"scene {i}: on-screen text is {len(text)} "
                          f"characters (max {MAX_ON_SCREEN})")
        if not (scene.get("narration") or "").strip():
            errors.append(f"scene {i}: missing narration")
        duration = scene.get("duration_s", 5)
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            errors.append(f"scene {i}: duration_s is not a number")
            continue
        if duration < MIN_SCENE_S or duration > MAX_SCENE_S:
            errors.append(f"scene {i}: duration {duration}s "
                          f"(must be {MIN_SCENE_S}–{MAX_SCENE_S}s)")
        total += duration
    if total < MIN_TOTAL_S:
        errors.append(f"total {total:.0f}s (min {MIN_TOTAL_S}s)")
    if total > MAX_TOTAL_S:
        errors.append(f"total {total:.0f}s (max {MAX_TOTAL_S}s)")
    return errors


def total_duration(script: dict) -> float:
    return sum(float(s.get("duration_s", 5)) for s in script["scenes"])


def _fonts(size: int):
    from PIL import ImageFont

    for path in _FONT_PATHS:
        try:
            bold = ImageFont.truetype(_FONT_PATHS[0], size)
            regular = ImageFont.truetype(_FONT_PATHS[1], max(10, size - 8))
            return bold, regular
        except Exception:  # noqa: BLE001 - fall back to the built-in font
            continue
    default = ImageFont.load_default()
    return default, default


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_slide(scene: dict, index: int, total: int, title: str,
                 size: tuple[int, int] = SIZES["vertical"]) -> "object":
    """One scene -> one PIL image. Returns the Image (caller saves it)."""
    from PIL import Image, ImageDraw

    width, height = size
    img = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(img)
    margin = int(width * 0.07)
    # Violet accent bar + scene counter.
    draw.rectangle([0, 0, width, int(height * 0.012)], fill=VIOLET)
    head_size = max(28, int(width * 0.055))
    bold, regular = _fonts(head_size)
    draw.text((margin, int(height * 0.06)),
              f"{index + 1} / {total}", font=regular, fill=MUTED)
    title_lines = _wrap(draw, title, regular, width - 2 * margin)
    y = int(height * 0.10)
    for line in title_lines[:2]:
        draw.text((margin, y), line, font=regular, fill=MUTED)
        y += int(head_size * 1.1)
    # On-screen text, centred vertically.
    body_size = max(40, int(width * 0.085))
    body_bold, _ = _fonts(body_size)
    lines = _wrap(draw, scene["on_screen_text"], body_bold,
                  width - 2 * margin)
    line_h = int(body_size * 1.25)
    block_h = line_h * len(lines)
    y = (height - block_h) // 2
    for line in lines:
        w = draw.textlength(line, font=body_bold)
        draw.text(((width - w) / 2, y), line, font=body_bold, fill=INK)
        y += line_h
    # Footer.
    foot_size = max(24, int(width * 0.04))
    _, foot_font = _fonts(foot_size)
    draw.text((margin, int(height * 0.94)), SITE, font=foot_font,
              fill=VIOLET_DEEP)
    return img


def render_slides(script: dict, out_dir: Path,
                  size: tuple[int, int] = SIZES["vertical"]) -> list[Path]:
    """All scenes -> numbered PNG slides. Returns the paths in order."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, scene in enumerate(script["scenes"], 1):
        img = render_slide(scene, i - 1, len(script["scenes"]),
                           str(script["title"]), size=size)
        path = out_dir / f"scene_{i:02d}.png"
        img.save(path)
        paths.append(path)
    return paths


def _srt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(script: dict, path: Path) -> Path:
    """Narration -> SRT subtitles, timed to the scenes."""
    path = Path(path)
    start = 0.0
    blocks = []
    for i, scene in enumerate(script["scenes"], 1):
        duration = float(scene.get("duration_s", 5))
        blocks.append(f"{i}\n{_srt_timestamp(start)} --> "
                      f"{_srt_timestamp(start + duration)}\n"
                      f"{scene['narration'].strip()}\n")
        start += duration
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def render_video(script: dict, out_path: Path,
                 size: tuple[int, int] = SIZES["vertical"],
                 audio: Path | None = None,
                 ffmpeg: str = "ffmpeg") -> dict:
    """Validate, slide, subtitle, assemble. Returns render details.

    Raises ValueError on an invalid script and RuntimeError when
    ffmpeg is missing or the encode fails — never a silent fake.
    """
    errors = validate_script(script)
    if errors:
        raise ValueError("invalid video script: " + "; ".join(errors))
    if shutil.which(ffmpeg) is None:
        raise RuntimeError(
            f"ffmpeg not found ({ffmpeg!r}) — install ffmpeg to render video")
    try:
        from PIL import Image  # noqa: F401 - import check with a clear error
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is not installed — pip install -r requirements.txt "
            "to render video") from exc
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = total_duration(script)
    with tempfile.TemporaryDirectory(prefix="video_render_") as tmp:
        tmp_path = Path(tmp)
        slides = render_slides(script, tmp_path / "slides", size=size)
        concat = tmp_path / "concat.txt"
        concat.write_text("".join(
            f"file '{p}'\nduration {float(s.get('duration_s', 5))}\n"
            for p, s in zip(slides, script["scenes"]))
            + f"file '{slides[-1]}'\n", encoding="utf-8")
        # -t caps the output at exactly the scripted length: the
        # trailing repeated file in the concat list can otherwise
        # inherit the previous duration and stretch the video.
        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0",
               "-i", str(concat)]
        if audio is not None:
            cmd += ["-i", str(audio)]
        cmd += ["-vf", "fade=t=in:st=0:d=0.3,fade=t=out:st="
                f"{max(0.0, duration - 0.3):.2f}:d=0.3,fps=30",
                "-pix_fmt", "yuv420p"]
        if audio is not None:
            # No -shortest: the video runs its full scripted length and
            # a shorter voiceover simply ends early. -shortest would cut
            # scenes when the voiceover runs short.
            cmd += ["-c:a", "aac"]
        cmd += ["-c:v", "libx264", "-t", f"{duration:.2f}", str(out_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                "ffmpeg failed: " + (proc.stderr or "")[-500:])
    srt_path = out_path.with_suffix(".srt")
    write_srt(script, srt_path)
    return {"video": str(out_path), "srt": str(srt_path),
            "duration_s": round(duration, 2),
            "scenes": len(script["scenes"]), "size": list(size),
            "audio": bool(audio)}


def load_script(path: Path) -> dict:
    """Load a script JSON file (raises ValueError on bad JSON)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read script {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"script {path} is not a JSON object")
    return data
