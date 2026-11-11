"""The jobs a customer can run on one of their websites ("Run agent work").

Every job in CREDIT_COSTS is runnable here, on that site's URL, country
and language — none is hard-wired to one business. Each job:

1. ``preflight`` — checks the inputs the job needs and what this server
   must have (an AI key for drafting, ffmpeg for video). A problem here
   is reported BEFORE any credit is charged.
2. ``run`` — does the work and writes a Markdown report (plus the
   video / chart files for the jobs that make them). Raises on
   failure; the caller refunds the credits.

Nothing is published or posted by any job: articles, replies and videos
are drafts and files for the owner to review and post by hand.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

# (usd, tokens) hard caps per agent task — same caps as the growth
# programs use for the same steps.
_BUDGETS = {
    "seo_keywords": (1.0, 4000),
    "seo_article": (3.0, 24000),
    "content": (2.0, 20000),
}

#: Shown in the job picker, in this order.
JOB_LABELS = {
    "analyze": "Analyze my site — site + GEO audit",
    "geo_audit": "GEO audit — AI answer-engine readiness",
    "community_listening": "Community listening — Reddit threads, past week",
    "social_report": "Social media report — from your export file",
    "seo_article": "SEO article draft",
    "video_render": "Video render — script to MP4 + subtitles",
    "x_campaign": "X campaign — posts drafted for your approval",
}

#: What each job asks for beyond the site itself (shown as hints).
JOB_HINTS = {
    "community_listening": "Needs keywords; communities are optional "
                           "(all of Reddit by default).",
    "social_report": "Needs a CSV or JSON export from your social "
                     "platform.",
    "seo_article": "Optional keywords to target; otherwise Ventoryn "
                   "maps them from your site.",
    "video_render": "Ventoryn drafts the script from your site, then "
                    "renders it — with an ElevenLabs voiceover, or a HeyGen "
                    "presenter, when the server has those keys.",
    "x_campaign": "30 days, one post a day, each linking to a page of your "
                  "site. Nothing is posted until you approve it in Approvals.",
}

MAX_UPLOAD_BYTES = 5_000_000


class JobInputError(ValueError):
    """The request is missing something the job needs (400, no charge)."""


class JobUnavailable(RuntimeError):
    """This server cannot run the job right now (503, no charge)."""


def _llm_enabled() -> bool:
    try:
        from core.llm import LLMGateway
        return bool(LLMGateway.enabled())
    except Exception:  # noqa: BLE001 - no LLM layer means no LLM
        return False


def server_blocker(job_type: str) -> str | None:
    """Why this server cannot run the job at all, or None. Short — it
    is shown in the job picker; the full fix is in the run message."""
    if job_type in ("seo_article", "video_render", "x_campaign") and not _llm_enabled():
        return "needs an AI key on the server"
    if job_type == "video_render":
        if shutil.which("ffmpeg") is None:
            return "needs ffmpeg installed on the server"
        try:
            import PIL  # noqa: F401
        except ImportError:
            return "needs Pillow installed on the server"
    return None


def parse_list(value: str | None) -> list[str]:
    return [v.strip() for v in re.split(r"[,\n]", value or "") if v.strip()]


def preflight(job_type: str, inputs: dict) -> None:
    """Raise JobInputError / JobUnavailable before anything is charged."""
    blocker = server_blocker(job_type)
    if blocker:
        fix = (" (set OPENAI_API_KEY, ANTHROPIC_API_KEY or GEMINI_API_KEY "
               "and restart)" if "AI key" in blocker else "")
        raise JobUnavailable(f"{JOB_LABELS.get(job_type, job_type)} — "
                             f"{blocker}{fix}. Nothing was charged.")
    if job_type == "video_render" and inputs.get("presenter") == "heygen":
        from core.media_providers import presenter_blocker

        why = presenter_blocker()
        if why:
            raise JobUnavailable(f"HeyGen presenter — {why}. Nothing was charged.")
    if job_type == "community_listening" and not parse_list(inputs.get("keywords")):
        raise JobInputError("Community listening needs at least one keyword "
                            "(comma-separated). Nothing was charged.")
    if job_type == "social_report":
        data = inputs.get("export_bytes") or b""
        if not data:
            raise JobInputError("The social media report needs your export "
                                "file (CSV or JSON). Nothing was charged.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise JobInputError("That export is over 5 MB. Nothing was "
                                "charged.")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or "site"


def _host(site: dict) -> str:
    return site.get("host") or re.sub(r"^https?://", "", site["url"]).split("/")[0]


def _dispatch(orchestrator, agent_type, action, inputs, budget, criteria):
    usd, tokens = _BUDGETS[budget]
    task = orchestrator.submit(agent_type, inputs={"action": action, **inputs},
                               business_id=None, budget_usd=usd,
                               budget_tokens=tokens,
                               acceptance_criteria=criteria)
    return orchestrator.dispatch(task.id)


def _write(reports_dir: Path, name: str, lines: list[str]) -> str:
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return name


# -- the jobs -----------------------------------------------------------------

def _run_analyze(site, inputs, ctx):
    from ecosystem.analyze import render_report, run_analysis, slug_for_url

    result = run_analysis(site["url"], site["country"], site["language"])
    name = f"analyze_{slug_for_url(site['url'])}_{ctx['date']}.md"
    ctx["reports_dir"].mkdir(parents=True, exist_ok=True)
    (ctx["reports_dir"] / name).write_text(render_report(result),
                                           encoding="utf-8")
    summary = (f"Site {result['site_score']}/100 · GEO {result['geo_score']}/100 · "
               f"{len(result['findings'])} findings · report {name}")
    return summary, name


def _run_geo(site, inputs, ctx):
    from core.geo_audit import GeoAuditor

    result = GeoAuditor().audit(site["url"])
    lines = [f"# GEO audit — {site['url']} ({ctx['date']})", "",
             f"GEO readiness score: **{result['score']}/100** "
             f"({result['pages_checked']} pages checked, "
             f"{len(result['findings'])} findings)", "",
             "llms.txt: " + ("present" if result.get("has_llms_txt") else "missing"),
             ""]
    robots = result.get("robots") or {}
    lines.append("AI crawlers: " + (", ".join(f"{b}={s}" for b, s in robots.items())
                                    if robots else "robots.txt unreadable"))
    lines += ["", "## Findings", ""]
    for f in result["findings"]:
        lines.append(f"- **{f.get('severity', '').upper()}** "
                     f"[{f.get('area', '')}] {f.get('issue', '')}"
                     + (f" — fix: {f['fix']}" if f.get("fix") else ""))
    if not result["findings"]:
        lines.append("- No GEO issues found.")
    name = _write(ctx["reports_dir"],
                  f"geo_{_slug(_host(site))}_{ctx['date']}.md", lines)
    return (f"GEO {result['score']}/100 · {len(result['findings'])} findings · "
            f"report {name}"), name


def _run_listening(site, inputs, ctx):
    from core.community_listening import listen_reddit

    keywords = parse_list(inputs.get("keywords"))
    communities = [c if c.lower().startswith("r/") else f"r/{c}"
                   for c in parse_list(inputs.get("community"))] or ["r/all"]
    result = listen_reddit(communities, keywords, now=ctx["now"])
    reddit = result["sources"]["reddit"]
    if reddit.get("status") != "ok":
        raise RuntimeError(f"Reddit unavailable: {reddit.get('reason', 'unknown')}")
    threads = result["threads"]
    lines = [f"# Community listening — {site['url']} ({ctx['date']})", "",
             "> **NOTHING WAS POSTED.** These are threads worth a look; any "
             "reply is yours to write and post.", "",
             f"Communities: {', '.join(communities)} · Keywords: "
             f"{', '.join(keywords)} · {reddit.get('queries', 0)} searches", "",
             "## Threads", ""]
    for t in threads:
        lines.append(f"- [{t.get('title', '')}]({t.get('url', '')}) — "
                     f"{t.get('venue', '')} · intent {t.get('intent_score', '?')}/100 "
                     f"({t.get('intent', '')})")
    if not threads:
        lines.append("- No matching threads in the past week.")
    name = _write(ctx["reports_dir"],
                  f"listening_{_slug(_host(site))}_{ctx['date']}.md", lines)
    return f"{len(threads)} threads found · report {name}", name


def _run_social(site, inputs, ctx):
    from ecosystem.social_analytics import run_report

    upload_dir = ctx["reports_dir"] / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(inputs.get("export_name") or "export.csv").name
    suffix = ".json" if filename.lower().endswith(".json") else ".csv"
    data_path = upload_dir / f"social_{_slug(_host(site))}_{ctx['stamp']}{suffix}"
    data_path.write_bytes(inputs["export_bytes"])
    out = run_report(data_path, channel=None,
                     title=f"Social media report — {_host(site)} ({ctx['date']})",
                     reports_root=ctx["reports_dir"], now=ctx["now"])
    name = Path(out["md"]).name
    skipped = f" · {out['skipped']} rows skipped" if out.get("skipped") else ""
    return f"Social report ready{skipped} · report {name}", name


def _site_profile(site) -> tuple[dict, list[str], object]:
    from core.seo_content import fetch_inventory

    inventory = fetch_inventory(site["url"])
    titles = [p.title for p in inventory.pages if getattr(p, "title", "")][:12]
    profile = {"site_url": site["url"], "what": f"The website {site['url']}",
               "market": site["country"], "language": site["language"],
               "page_titles": titles}
    return profile, titles, inventory


def _run_seo(site, inputs, ctx):
    from core.seo_content import suggest_internal_links
    from core.seo_publish import FileExportPublisher
    from orchestrator.models import TaskStatus

    profile, titles, inventory = _site_profile(site)
    niche = titles[0] if titles else _host(site)
    seeds = parse_list(inputs.get("keywords")) or [w for w in titles[:3]] or [niche]
    run = _dispatch(ctx["orchestrator"], "marketing", "seo_keyword_map",
                    {"niche": niche, "seeds": seeds}, "seo_keywords",
                    ["at least 1 keyword returned",
                     "no invented numbers — gaps marked 'unavailable'"])
    if run.status != TaskStatus.COMPLETED:
        raise RuntimeError(f"keyword mapping failed: {run.error}")
    keywords = run.output.get("keywords") or []
    if not keywords:
        raise RuntimeError("no keywords mapped for this site")
    keyword = sorted(keywords, key=lambda k: (k.get("avg_12mo") is None,
                                              -(k.get("avg_12mo") or 0)))[0]["keyword"]
    candidates = suggest_internal_links(keyword, inventory, limit=8)
    have = {c["url"] for c in candidates}
    for page in inventory.pages:
        if len(candidates) >= 3:
            break
        if page.url not in have:
            candidates.append({"url": page.url, "title": page.title})
    run = _dispatch(ctx["orchestrator"], "creative", "seo_article",
                    {"keyword": keyword, "niche": niche, "site_url": site["url"],
                     "facts": [profile["what"], *titles[:6]],
                     "link_candidates": candidates,
                     "inventory_urls": sorted(inventory.urls),
                     "word_target": 1200},
                    "seo_article",
                    ["article package has title, meta_description, slug, "
                     "body_markdown, faq, internal_links",
                     "internal links only from the live site inventory"])
    if run.status != TaskStatus.COMPLETED:
        raise RuntimeError(f"article drafting failed: {run.error}")
    pkg = run.output["package"]
    out = FileExportPublisher(ctx["reports_dir"] / "seo" / _slug(_host(site))
                              ).publish(pkg, site["url"])
    lines = [f"# SEO article draft — {site['url']} ({ctx['date']})", "",
             "> **NOTHING WAS PUBLISHED.** Review the draft, then publish "
             "it yourself.", "",
             f"- Keyword: {keyword}", f"- Title: {pkg.get('title', '')}",
             f"- Draft: `{out['markdown']}`"]
    name = _write(ctx["reports_dir"],
                  f"seo_{_slug(_host(site))}_{ctx['stamp']}.md", lines)
    return f"Article drafted for “{keyword}” · report {name}", name


def _make_video(site, script, videos, stem, inputs, ctx):
    """Render the video. With a HeyGen presenter requested, HeyGen makes
    it; otherwise Ventoryn renders the slides, with an ElevenLabs
    voiceover when the server has it (and the owner did not turn it
    off). Paid usage is recorded for the website's Costs."""
    from core import media_providers as mp
    from core.video_render import SIZES, render_video, validate_script

    store = ctx.get("store")
    narration = " ".join(str(s.get("narration") or "") for s in script["scenes"])

    def record(provider, units, unit, cost):
        if store is not None:
            store.record_media_usage(site["account_id"], site["id"], provider,
                                     units, unit, cost, job_ref=stem)

    def priced(cost):
        return f"${cost:.2f}" if cost is not None else "price not set"

    if inputs.get("presenter") == "heygen":
        made = mp.heygen_presenter_video(narration, videos / f"{stem}.mp4",
                                         title=script["title"])
        record("heygen", made["seconds"], "seconds", made["cost_usd"])
        return ({"video": made["path"], "srt": "", "duration_s": round(made["seconds"], 1),
                 "scenes": len(script["scenes"])},
                [f"Presenter: HeyGen avatar, {made['seconds']:.0f}s ({priced(made['cost_usd'])})"])
    extras = []
    audio = None
    if str(inputs.get("voiceover", "on")) != "off" and mp.voice_ready():
        voice = mp.elevenlabs_voiceover(narration, videos / f"{stem}.mp3")
        record("elevenlabs", voice["characters"], "characters", voice["cost_usd"])
        seconds = mp.audio_seconds(Path(voice["path"]))
        if seconds:
            mp.fit_scenes_to_audio(script, seconds)
            errors = validate_script(script)
            if errors:
                raise RuntimeError("voiceover too long for the video: " + "; ".join(errors))
        audio = Path(voice["path"])
        extras.append(f"Voiceover: ElevenLabs, {voice['characters']:,} characters "
                      f"({priced(voice['cost_usd'])})")
    rendered = render_video(script, videos / f"{stem}.mp4", size=SIZES["vertical"],
                            audio=audio)
    return rendered, extras


def _run_video(site, inputs, ctx):
    from core.video_render import SIZES, render_video, validate_script
    from orchestrator.models import TaskStatus

    profile, titles, _inv = _site_profile(site)
    experiment = {"name": f"Intro video for {_host(site)}",
                  "channel": "short-form video",
                  "hypothesis": "A short explainer brings new visitors.",
                  "content_needed": ["video-script"]}
    run = _dispatch(ctx["orchestrator"], "creative", "content_pieces",
                    {"experiment": experiment, "profile": profile}, "content",
                    ["one video-script draft"])
    if run.status != TaskStatus.COMPLETED:
        raise RuntimeError(f"script drafting failed: {run.error}")
    pieces = [p for p in run.output.get("pieces", [])
              if p.get("type") == "video-script"]
    if not pieces:
        raise RuntimeError("the creative agent returned no video script")
    script = {"title": pieces[0].get("title", experiment["name"]),
              "scenes": pieces[0].get("scenes", [])}
    errors = validate_script(script)
    if errors:
        raise RuntimeError("invalid video script: " + "; ".join(errors))
    videos = ctx["reports_dir"] / "videos"
    videos.mkdir(parents=True, exist_ok=True)
    stem = f"{_slug(_host(site))}_{ctx['stamp']}"
    rendered, extras = _make_video(site, script, videos, stem, inputs, ctx)
    lines = [f"# Video — {script['title']} ({ctx['date']})", "",
             "> **NOT POSTED.** The MP4 is a file for you to post by hand.", "",
             f"- Video: `{rendered['video']}`",
             *([f"- Subtitles: `{rendered['srt']}`"] if rendered.get("srt") else []),
             f"- {rendered['duration_s']}s · {rendered['scenes']} scenes · vertical",
             *[f"- {x}" for x in extras],
             "", "## Scenes", ""]
    for i, scene in enumerate(script["scenes"], 1):
        lines.append(f"{i}. **{scene.get('on_screen_text', '')}** — "
                     f"{str(scene.get('narration', ''))[:120]}")
    name = _write(ctx["reports_dir"], f"video_{stem}.md", lines)
    return (f"Video rendered ({rendered['duration_s']}s, "
            f"{rendered['scenes']} scenes) · report {name}"), name


def _run_x_campaign(site, inputs, ctx):
    """Draft an X campaign and save it as a proposal (nothing posted)."""
    from ventoryn import campaigns
    from ventoryn.campaign_cost import X_RATE_LINKED_POST_USD, X_RATE_PLAIN_POST_USD

    store = ctx.get("store")
    if store is None:
        raise RuntimeError("campaign drafting needs the account store")
    days = max(1, min(int(inputs.get("days") or 30), 365))
    ppd = max(0.1, min(float(inputs.get("posts_per_day") or 1), 4))
    with_link = str(inputs.get("with_link", "1")) not in ("0", "", "false", "off")
    posts = campaigns.draft_posts(site, days=days, posts_per_day=ppd,
                                  with_link=with_link)
    campaign = campaigns.propose(store, site, posts, days=days, posts_per_day=ppd,
                                 with_link=with_link,
                                 source=inputs.get("source") or "manual")
    rate = X_RATE_LINKED_POST_USD if with_link else X_RATE_PLAIN_POST_USD
    lines = [f"# X campaign proposal — {site['url']} ({ctx['date']})", "",
             "> **NOTHING WAS POSTED.** Review and approve it in Approvals.", "",
             f"- {len(posts)} posts over {days} days — about ${len(posts) * rate:.2f} on X",
             "", "## Posts", ""]
    lines += [f"{i}. {p['text']}" + (f" — {p['link']}" if p["link"] else "")
              for i, p in enumerate(posts, 1)]
    name = _write(ctx["reports_dir"], f"campaign_{_slug(_host(site))}_{ctx['stamp']}.md", lines)
    return (f"Campaign proposed: {len(posts)} X posts over {days} days, about "
            f"${len(posts) * rate:.2f} on X — review it in Approvals · report {name}"), name


_RUNNERS = {
    "x_campaign": _run_x_campaign,
    "analyze": _run_analyze,
    "geo_audit": _run_geo,
    "community_listening": _run_listening,
    "social_report": _run_social,
    "seo_article": _run_seo,
    "video_render": _run_video,
}

RUNNABLE = frozenset(_RUNNERS)


def run(job_type: str, site: dict, inputs: dict, *, orchestrator,
        reports_dir: Path, now: datetime | None = None,
        store=None) -> tuple[str, str]:
    """Run one job. Returns (summary, report_name); raises on failure.
    ``store`` is the account store, for jobs that save drafts (campaigns)."""
    now = now or datetime.now(timezone.utc)
    ctx = {"now": now, "date": now.strftime("%Y-%m-%d"),
           "stamp": now.strftime("%Y%m%d-%H%M%S"),
           "reports_dir": Path(reports_dir), "orchestrator": orchestrator,
           "store": store}
    return _RUNNERS[job_type](site, inputs, ctx)
