# Video

HeyCatch generates up to 20 AI videos/slideshows a day. The
ecosystem can now do the same for its own sites — write the script,
render the slideshow, hand the file to the owner to post.

```bash
python businesses/road2cissp/growth.py video --script script.json
python businesses/road2cissp/growth.py video --script script.json --audio voiceover.mp3
python businesses/road2cissp/growth.py video --script script.json --format wide
python businesses/road2cissp/growth.py video --from-experiment reddit-answers
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

## Video ads (UGC) — the render-job slot

`ventoryn/video_ads.py` is the video-ad slot in the content engine
(Gap 5, Holo parity without a provider chosen):

- **Script/storyboard**: `generate_video_ad_script(site_url,
  brand_profile=None)` writes a short ad — hook, 4 scenes with
  visuals + voiceover lines, CTA — from the site URL and an optional
  plain-dict brand profile (`name`, `tagline`, `tone`, `audience`,
  `offer`, `cta`). Only brand facts actually supplied are used; an
  empty profile produces a generic script naming only the site host,
  never invented facts. `storyboard(script)` derives the shot list.
  Every script passes `validate_script` before it is returned.
- **Render jobs**: `VideoRenderProvider` is the injected provider
  interface (`render` / `status`, no live calls at import). Jobs live
  in `SaasStore` (`saas_video_ad_jobs`) with the lifecycle queued →
  rendering → ready | failed. With no provider configured a job sits
  honestly at `awaiting_provider`: nothing is rendered, no URL is
  stored. `HeyGenProvider` wraps the in-repo HeyGen v3 client
  (`core/media_providers.py`); it is never constructed by default and
  fails honestly without keys.
- **Draft-only**: a rendered video ad is a draft until a human
  approves it through the existing review record
  (`video_review_status` / `set_video_review`, the Approvals flow).
  `publish_video_ad(store, job_id)` is the only publish gate and
  refuses without an approval record. It posts nothing itself.

Provider comparison (public pricing, checked 2026-11-19 — re-verify
before any spend): **OpenAI Sora is shut down** (API sunset
2026-09-24), so it is not a choice. **HeyGen** (live, API
pay-as-you-go wallet from $5; Avatar III $0.60/min, Avatar IV
$2.31–$4.83/min, Avatar V $7.20/min; a 24s ad ≈ $0.24–$2.88) makes
the talking-presenter UGC ad and already has a client in the repo.
**Runway** (live, API $0.01/credit, $10 min top-up; Gen-4 Turbo
$0.05/s, Gen-4.5 $0.12/s; a 24s ad ≈ $1.20/$2.88) makes AI footage
from text or a product image, with no avatar presenter. No provider
is wired in; choosing one and approving its cost is Spencer's call.

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
- `businesses/road2cissp/growth.py` — `run_video`, the `video` CLI.
- `requirements.txt` — Pillow (slides); ffmpeg is a system binary.
