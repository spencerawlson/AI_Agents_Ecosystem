"""SEO engine mission: GrandRanker-class content autopilot, run as an
ecosystem business on Spencer's OWN sites first.

Pipeline (each step is a real agent task with a hard budget):
  1. Crawl the target site's sitemap -> live page inventory (real HTTP).
  2. marketing/seo_keyword_map: seed keywords scored on LIVE Google
     Trends data (gaps marked 'unavailable', never invented).
  3. creative/seo_article per chosen keyword: long-form article package
     (title, meta, slug, body, FAQ, internal links) written by the LLM
     against the site's real facts and real pages only.
  4. Deterministic quality gate (core.seo_content): hallucinated links,
     slug collisions with the live site, keyword stuffing, thin copy and
     placeholders are hard errors and block publishing.
  5. Publishing is a separate, explicit choice (--publish):
       none      -> drafts + report only (default; nothing leaves the box)
       export    -> draft article files written for a static/Next.js repo
       wordpress -> posts created via the WP REST API as DRAFTS
     Nothing is ever published live by this mission.

Usage (on the Fedora server):
    export OPENAI_API_KEY=...
    python3 ecosystem/seo_engine_mission.py \
        --site-url https://proofflow.me \
        --niche "proof of delivery for contractors and couriers" \
        --seeds "proof of delivery,delivery confirmation app,..." \
        --fact "ProofFlow is a free proof-of-delivery web tool" \
        --count 3 --publish export
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.models import BusinessStatus
from core.seo_content import fetch_inventory, suggest_internal_links, \
    word_count
from ecosystem.marketing_mission import _require_llm_ready, run_step
from ecosystem.runtime import build_runtime


def _banner(publish: str) -> str:
    if publish == "none":
        return ("> **NOTHING WAS PUBLISHED** — all articles below are "
                "drafts awaiting human approval.")
    if publish == "export":
        return ("> **DRAFT FILES EXPORTED** — articles were written as "
                "draft files for the site repo. Nothing went live.")
    return ("> **WORDPRESS DRAFTS CREATED** — posts exist as drafts in "
            "WordPress. Nothing went live.")


def write_report(path: Path, ctx: dict) -> None:
    kw_rows = "\n".join(
        f"| {k['keyword']} | "
        f"{k['avg_12mo'] if k.get('avg_12mo') is not None else 'n/a'} | "
        f"{k.get('trend', 'n/a')} |"
        for k in ctx["keywords"])
    art_blocks = []
    for a in ctx["articles"]:
        q = a["quality"]
        findings = [f"error: {e}" for e in q["errors"]] + \
                   [f"warning: {w}" for w in q["warnings"]]
        find_txt = "\n".join(f"  - {f}" for f in findings) or "  - clean"
        art_blocks.append(
            f"### {a['package'].get('title', '(untitled)')}\n\n"
            f"- keyword: {a['keyword']}\n"
            f"- words: {a['words']} · slug: {a['package'].get('slug', '')}\n"
            f"- gate: {'READY' if not q['errors'] else 'BLOCKED'}\n"
            f"{find_txt}\n"
            f"- publish: {a['publish_status']}")
    report = f"""# SEO Engine Mission — {ctx['site_url']}

_Generated {ctx['generated_at']} · niche: {ctx['niche']}_

{_banner(ctx['publish'])}

## Spend

Total AI spend this mission: **${ctx['total_spend_usd']:.4f}**
({ctx['total_tokens']} tokens across {ctx['steps_completed']} agent steps)

## Site inventory

{ctx['inventory_pages']} live pages crawled from the site sitemap —
internal links in every article resolve to this inventory.

## Keyword map (live Google Trends)

| Keyword | Avg interest | Trend |
|---------|--------------|-------|
{kw_rows}

## Articles

{chr(10).join(art_blocks) if art_blocks else '_No articles produced._'}
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="SEO engine mission (own sites first)")
    parser.add_argument("--site-url", required=True)
    parser.add_argument("--niche", required=True)
    parser.add_argument("--seeds", required=True,
                        help="comma-separated seed keywords")
    parser.add_argument("--name", default="",
                        help="business name (default: the domain)")
    parser.add_argument("--fact", action="append", default=[],
                        help="a true fact about the site/product the "
                             "writer may assert (repeatable)")
    parser.add_argument("--count", type=int, default=3,
                        help="articles to produce (top keywords)")
    parser.add_argument("--word-target", type=int, default=1500)
    parser.add_argument("--publish", choices=["none", "export", "wordpress"],
                        default="none")
    parser.add_argument("--export-dir", default="")
    parser.add_argument("--llm", dest="llm", action="store_true",
                        default=None)
    parser.add_argument("--no-llm", dest="llm", action="store_false")
    args = parser.parse_args(argv)

    if args.llm is not False:
        _require_llm_ready()

    domain = urlparse(args.site_url).netloc or args.site_url
    seeds = [s.strip() for s in args.seeds.split(",") if s.strip()]

    print(f"crawling sitemap: {args.site_url}", flush=True)
    try:
        inventory = fetch_inventory(args.site_url)
    except ValueError as exc:
        print(f"MISSION STOPPED: {exc}", flush=True)
        return 1
    print(f"inventory: {len(inventory.pages)} pages", flush=True)

    rt = build_runtime(use_llm=args.llm,
                     use_postgres=bool(os.environ.get("DATABASE_URL")),
                     approvals_persist=True)
    orch = rt.orchestrator
    biz = rt.businesses.create(
        name=args.name or domain, business_type="seo_content_engine",
        domain=domain)
    rt.businesses.transition(biz.id, BusinessStatus.RESEARCHING)
    print(f"business {biz.id} ({biz.name}) -> researching", flush=True)

    ctx: dict = {
        "site_url": args.site_url, "niche": args.niche,
        "publish": args.publish,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inventory_pages": len(inventory.pages),
        "keywords": [], "articles": [],
        "total_spend_usd": 0.0, "total_tokens": 0, "steps_completed": 0,
    }

    def track(run):
        ctx["total_spend_usd"] += run.cost_usd or 0.0
        ctx["total_tokens"] += run.tokens_used or 0

    ok, result, run = run_step(
        orch, "marketing", "seo_keyword_map",
        {"niche": args.niche, "seeds": seeds}, biz.id, 1.0, 4000,
        ["at least 1 keyword returned",
         "no invented numbers — gaps marked 'unavailable' explicitly"])
    track(run)
    if not ok:
        print(f"MISSION STOPPED: {result}", flush=True)
        return 1
    ctx["steps_completed"] += 1
    keywords = result["keywords"]
    ctx["keywords"] = keywords
    print(f"ok: marketing/seo_keyword_map ({len(keywords)} keywords)",
          flush=True)

    scored = sorted(
        keywords,
        key=lambda k: (k.get("avg_12mo") is None, -(k.get("avg_12mo") or 0)))
    chosen = scored[: max(1, args.count)]

    publishers = _build_publishers(args, domain)
    for entry in chosen:
        keyword = entry["keyword"]
        candidates = suggest_internal_links(keyword, inventory, limit=8)
        if len(candidates) < 3:  # writer needs >= 3 link options
            have = {c["url"] for c in candidates}
            for page in inventory.pages:
                if len(candidates) >= 3:
                    break
                if page.url not in have:
                    candidates.append({"url": page.url, "title": page.title})
        ok, result, run = run_step(
            orch, "creative", "seo_article",
            {"keyword": keyword, "niche": args.niche,
             "site_url": args.site_url, "facts": args.fact,
             "link_candidates": candidates,
             "inventory_urls": sorted(inventory.urls),
             "word_target": args.word_target},
            biz.id, 3.0, 24000,
            ["article package has title, meta_description, slug, "
             "body_markdown, faq, internal_links",
             "internal links only from the live site inventory"])
        track(run)
        if not ok:
            print(f"MISSION STOPPED at {keyword!r}: {result}", flush=True)
            _finish(rt, biz, ctx)
            return 1
        ctx["steps_completed"] += 1
        pkg = result["package"]
        quality = result["quality"]
        status = "not published (--publish none)"
        if not quality["errors"] and args.publish != "none":
            status = publishers(args.publish, pkg)
        elif quality["errors"]:
            status = "blocked by quality gate"
        ctx["articles"].append({
            "keyword": keyword, "package": pkg, "quality": quality,
            "words": word_count(str(pkg.get("body_markdown", ""))),
            "publish_status": status})
        print(f"ok: creative/seo_article {keyword!r} "
              f"(gate errors: {len(quality['errors'])}, {status})",
              flush=True)

    _finish(rt, biz, ctx)
    return 0


def _build_publishers(args, domain: str):
    """Return a callable(mode, pkg) -> status string. Built lazily so a
    --publish none run never touches publisher config."""
    export_dir = args.export_dir or str(
        Path("reports") / "seo" / domain.replace(".", "_"))

    def publish(mode: str, pkg: dict) -> str:
        from core.seo_publish import FileExportPublisher, PublisherError, \
            WordPressPublisher
        try:
            if mode == "export":
                out = FileExportPublisher(export_dir).publish(
                    pkg, args.site_url, author=args.name or domain)
                return f"exported draft: {out['markdown']}"
            import os
            pub = WordPressPublisher(
                os.environ.get("WP_SITE_URL", args.site_url),
                os.environ.get("WP_USER", ""),
                os.environ.get("WP_APP_PASSWORD", ""))
            out = pub.create_post(pkg, status="draft")
            return f"wordpress draft #{out['id']}: {out['link']}"
        except PublisherError as exc:
            return f"publish failed: {exc}"

    return publish


def _finish(rt, biz, ctx: dict) -> None:
    report_path = Path("reports") / (
        f"seo_engine_{urlparse(ctx['site_url']).netloc.replace('.', '_')}"
        f"_{datetime.now(timezone.utc):%Y%m%d}.md")
    write_report(report_path, ctx)
    ready = sum(1 for a in ctx["articles"] if not a["quality"]["errors"])
    print(f"\n{_banner(ctx['publish'])}", flush=True)
    print(f"report: {report_path}", flush=True)
    print(f"articles: {len(ctx['articles'])} produced, {ready} gate-ready",
          flush=True)
    print(f"total AI spend: ${ctx['total_spend_usd']:.4f} "
          f"({ctx['total_tokens']} tokens)", flush=True)
    approval = rt.approvals.request(
        action="mission_report_approval",
        details={
            "mission": "seo_engine_mission",
            "business_name": biz.name,
            "site_url": ctx["site_url"],
            "report_path": str(report_path),
            "report_name": report_path.name,
            "articles_produced": len(ctx["articles"]),
            "articles_gate_ready": ready,
            "publish_mode": ctx["publish"],
            "total_spend_usd": round(ctx["total_spend_usd"], 4),
            "total_tokens": ctx["total_tokens"],
            "steps_completed": ctx["steps_completed"],
        },
        amount_usd=round(ctx["total_spend_usd"], 4),
        business_id=biz.id,
        requested_by="seo_engine_mission",
    )
    print(f"approval requested: {approval.id} — review it at /approvals",
          flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
