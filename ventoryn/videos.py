"""Agent-made videos: find a job's MP4 safely, and review them in Approvals.

A finished video_render job writes a report whose "- Video: `<path>`"
line names the MP4. The path is only trusted when it resolves inside the
reports' videos folder — a crafted report can never serve another file.

Review states (saas_video_reviews): pending (default) -> approved |
rejected. Approving does not post anything: it marks the video as the
one to use, and the owner downloads it (TikTok posting comes later).
"""

from __future__ import annotations

import html
import re
from pathlib import Path


def video_path(job: dict, reports_dir: Path) -> Path | None:
    """The MP4 of a finished video_render job, or None."""
    if not job or job.get("job_type") != "video_render" or job.get("status") != "done":
        return None
    m = re.search(r"report (\S+\.md)", job.get("result") or "")
    if not m:
        return None
    reports_dir = Path(reports_dir).resolve()
    report = (reports_dir / m.group(1)).resolve()
    if report.parent != reports_dir or not report.is_file():
        return None
    v = re.search(r"- Video: `([^`]+\.mp4)`", report.read_text(encoding="utf-8", errors="replace"))
    if not v:
        return None
    path = Path(v.group(1)).resolve()
    if (reports_dir / "videos") not in path.parents or not path.is_file():
        return None
    return path


def videos_for_review(store, account_id: str, reports_dir: Path, limit: int = 12) -> list[dict]:
    """Finished videos of the account, newest first, with their review state."""
    out = []
    for job in store.list_jobs(account_id, limit=80):
        if job["job_type"] != "video_render" or job["status"] != "done":
            continue
        if video_path(job, reports_dir) is None:
            continue
        out.append({**job, "review": store.video_review_status(job["id"])})
        if len(out) >= limit:
            break
    return out


def _esc(v) -> str:
    return html.escape(str(v), quote=True)


def review_section(store, account_id: str, reports_dir: Path, hosts: dict) -> str:
    vids = [v for v in videos_for_review(store, account_id, reports_dir) if v["review"] != "rejected"]
    if not vids:
        return ""
    vids.sort(key=lambda v: v["created_at"], reverse=True)  # newest first
    cards = ""
    for v in vids:
        sid, jid = _esc(v["site_id"]), _esc(v["id"])
        src = f"/websites/sites/{sid}/videos/{jid}"
        summary = (v.get("result") or "").replace("Automatic — ", "").split(" · report")[0]
        state = ("<span class='badge badge-ok'>Approved</span>" if v["review"] == "approved"
                 else "<span class='badge'>Waiting for review</span>")
        if v["review"] == "pending":
            actions = (f"<form method='post' action='{src}/review' class='v-video-actions'>"
                       f"<button class='btn btn-primary' name='decision' value='approve'>Approve</button> "
                       f"<button class='btn' name='decision' value='reject'>Reject</button></form>")
        elif v["review"] == "approved":
            # The Post link is only live when TikTok is actually
            # connected for this site — otherwise it is a dead end
            # (the post form answers "not connected").
            tiktok = store.get_connection(v["site_id"], "tiktok") or {}
            if tiktok.get("status") == "connected":
                actions = (f"<p class='v-video-actions'><a class='btn btn-primary' href='{src}/tiktok'>"
                           f"Post to TikTok</a></p>")
            else:
                actions = ("<p class='v-video-actions muted'>"
                           "Connect TikTok for this site to post it.</p>")
        else:
            actions = ""
        cards += (f"<div class='card v-video' id='video-{jid}'>"
                  f"<video controls preload='metadata' playsinline src='{src}'></video>"
                  f"<div><h3>{_esc(hosts.get(v['site_id'], ''))} {state}</h3>"
                  f"<p class='muted'>{_esc(summary)} — {_esc(str(v['created_at'])[:10])}</p>"
                  f"{actions}<p><a href='{src}?download=1'>Download MP4</a></p></div></div>")
    return ("<style>.v-video{display:grid;grid-template-columns:minmax(160px,240px) 1fr;gap:18px;"
            "align-items:start}.v-video video{width:100%;border-radius:12px;background:#000;"
            "aspect-ratio:9/16}.v-video h3{margin:0 0 6px}.v-video-actions{margin:10px 0 0}"
            "@media(max-width:560px){.v-video{grid-template-columns:1fr}}</style>"
            "<h2>Videos to review</h2><p class='muted'>Made by the agents, never posted. "
            "Watch, approve the ones worth posting, and download them.</p>" + cards)
