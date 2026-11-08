# Video

HeyCatch generates up to 20 AI videos/slideshows a day. The
ecosystem can now do the same for its own sites — write the script,
render the slideshow, hand the file to the owner to post.

```bash
python ecosystem/road2cissp_growth.py video --script script.json
python ecosystem/road2cissp_growth.py video --script script.json --audio voiceover.mp3
python ecosystem/road2cissp_growth.py video --script script.json --format wide
python ecosystem/road2cissp_growth.py video --from-experiment reddit-answers
```

## The script

A script is a JSON object both the writer and the renderer share:

```json
{"title": "CISSP in 60 seconds",
 "scenes": [{"on_screen_text": "Domain 3 is about models.",
             "narration": "Domain three is where the security models live...",
             "duration_s": 6}]}
```

Rules, enforced by `validate_script` before anything renders:
3–8 scenes, each with on-screen text (max 140 characters — it has
to fit on a phone screen) and narration, each 3–15 seconds, total
15–90 seconds. An invalid script is rejected with the reasons
listed, never rendered anyway.

`--from-experiment` drafts the script with the creative agent
(`content_pieces`, new type `video-script`, cheap tier) for an
**active** experiment, then renders it. The draft is validated the
same way; a script that fails the gate is reported, not rendered.

## The render

- **Slides** (Pillow): 1080x1920 vertical by default — TikTok /
  Reels / Shorts — or 1920x1080 wide for YouTube. Light design, the
  Road to CISSP direction: white, near-black text, violet accent
  bar, the site name as a footer. No stock photos, no generated
  imagery — the words are the video.
- **MP4** (ffmpeg, libx264): one slide per scene at its scripted
  duration, fade in/out, 30 fps. Needs ffmpeg on PATH.
- **Voiceover** (`--audio`): an mp3/wav file is muxed in — the
  owner's own recording (Spencer records his narration himself) or
  any audio. Without one the video is silent and the SRT carries
  the words. The video always runs its full scripted length; a
  shorter voiceover simply ends early.
- **SRT subtitles** from the narration, timed to the scenes, written
  next to the MP4 — most social video is watched muted.

Output: `reports/videos/<slug>.mp4` + `.srt`, a report at
`reports/video_<yyyymmdd>_<slug>.md`, and an entry in the growth
state under `videos` (`rendered`).

## What it does not do

No AI voice (no TTS key in the repo), no stock or AI-generated
footage, no auto-posting and no scheduling. A rendered video is a
file; the owner posts it by hand. Scheduling a video into the
posting queue is the follow-up; the content calendar can already
name a video-script day.

## Where it lives

- `core/video_render.py` — validation, slides, SRT, ffmpeg assembly.
  No network, no LLM.
- `agents/creative/agent.py` — `video-script` in `content_pieces`.
- `ecosystem/road2cissp_growth.py` — `run_video`, the `video` CLI.
- `requirements.txt` — Pillow (slides); ffmpeg is a system binary.
