"""Road to CISSP growth program: the agents own road2cissp.com's marketing.

This is the ecosystem's main focus. Each worker tick runs whatever jobs
are due; the agents research, plan, produce and measure continuously,
and the owner approves which experiments run and publishes the drafts.

    Job        Agent                       Cadence   Needs LLM
    audit      marketing/site_audit        7 days    no  (live crawl)
    keywords   marketing/seo_keyword_map   7 days    no  (Google Trends)
    metrics    Plausible Stats API         1 day     no  (optional key)
    strategy   marketing/growth_strategy   7 days    yes (smart tier)
    content    creative/content_pieces     once per approved experiment

Loop: the strategy proposes experiments (hypothesis, KPI, target,
duration, budget, stop rule) → each becomes a `marketing_experiment`
approval → approved ones go active and get content drafted → when the
duration ends they are marked for review, and the outcome (measured
metrics + the owner's note) feeds the next week's strategy.

NOTHING IS PUBLISHED OR SPENT AUTOMATICALLY. Drafts are files under
reports/road2cissp_drafts/; the weekly report is reports/road2cissp_growth_<date>.md.

CLI:
    python ecosystem/road2cissp_growth.py run [--force all|audit,strategy,...]
    python ecosystem/road2cissp_growth.py status
    python ecosystem/road2cissp_growth.py daily-plan [--date YYYY-MM-DD]
    python ecosystem/road2cissp_growth.py daily-plan-done <item-id>
    python ecosystem/road2cissp_growth.py daily-plan-skip <item-id>
    python ecosystem/road2cissp_growth.py record-metrics --visitors 420 --signups 12
    python ecosystem/road2cissp_growth.py outcome <slug> --worked "r/cissp answers drove 60 visits"
    python ecosystem/road2cissp_growth.py seo [--count 2]
    python ecosystem/road2cissp_growth.py listen [--limit 5]
    python ecosystem/road2cissp_growth.py record-revenue --amount 190 --note "10 subs, Oct"
    python ecosystem/road2cissp_growth.py record-cost --amount 12 --label "Domain renewal"
    python ecosystem/road2cissp_growth.py pnl

The seo subcommand runs the gated SEO article pipeline against the
live sitemap (same engine as the Farelune program): keyword map, then
articles that must pass the deterministic quality gate, exported as
drafts under reports/seo/road2cissp_com/ — never published. Revenue is
Stripe subscription money, booked by hand from the Stripe dashboard
(owner-reported, source named in the entry) until an API sync exists.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ecosystem.marketing_mission import SEED_KEYWORDS, SITE_FEATURES
from orchestrator.models import ApprovalStatus, TaskStatus

BUSINESS_NAME = "Road to CISSP"
SITE_URL = "https://road2cissp.com"

# Facts the strategy and content agents build on. Keep this accurate —
# the prompts forbid inventing anything beyond it.
SITE_PROFILE = {
    "site_url": SITE_URL,
    "name": BUSINESS_NAME,
    "what": "Free online learning platform taking learners from IT "
            "fundamentals to the CISSP: networking, security, Python, cloud "
            "and cybersecurity.",
    "pricing": "Freemium: a free account covers 3 lessons and 2 hands-on "
               "labs; full access to every track, lab and the AI tutor is a "
               "$19/month subscription (Stripe). Sign-in is Google/GitHub "
               "OAuth.",
    "features": SITE_FEATURES + [
        "AI study tutor (hints, explanations, weak-spot analysis) for signed-in learners",
        "CISSP roadmap, learning paths and courses → modules → lessons",
        "Brilliant-style guided lessons mixing questions with hands-on puzzles",
    ],
    "public_pages": ["/", "/academy/roadmap", "/academy/paths", "/academy/courses",
                     "/academy/exam", "/library", "/lab", "/labs", "/practice"],
    "goal": "Grow organic traffic, free sign-ups and $19/month "
            "subscriptions, with learners who keep coming back.",
    "known_competitors": [
        "Destination Certification", "Thor Teaches", "Pete Zerger (Inside Cloud "
        "and Security)", "Boson ExSim", "Pocket Prep", "Official ISC2 study "
        "guide / practice tests (Sybex)", "Cybrary",
    ],
    "communities": ["r/cissp", "r/cybersecurity", "r/ITCareerQuestions",
                    "r/CompTIA", "r/ccna", "LinkedIn", "Discord study groups"],
    "tech": "Vite + React SPA on Vercel with a FastAPI backend.",
}

GROWTH_KEYWORDS = SEED_KEYWORDS + [
    "free cissp course", "cissp study plan", "cissp exam tips",
    "free cybersecurity labs", "free networking labs", "subnetting practice",
]

CADENCE = {
    "audit": timedelta(days=7),
    "keywords": timedelta(days=7),
    "metrics": timedelta(days=1),
    "strategy": timedelta(days=7),
}
# After a failed attempt, wait this long before retrying (a broken key must
# not re-spend on every 60s tick).
RETRY_AFTER = timedelta(hours=6)

BUDGETS = {  # (usd, tokens) hard caps per agent task
    "audit": (0.5, 5000),
    "keywords": (1.0, 4000),
    "strategy": (3.0, 30000),
    "content": (2.0, 20000),
    "seo_keywords": (1.0, 4000),
    "seo_article": (3.0, 24000),
    "community": (2.0, 20000),
}

# Keywords the listener searches for, in priority order. The query
# budget (10 calls per run) interleaves subreddits and keywords, so a
# run covers every subreddit for the first two keywords; widening the
# set is a one-line change here.
LISTEN_KEYWORDS = [
    "cissp study plan",
    "how to pass cissp",
    "cissp practice questions",
    "security+ study guide",
    "free cybersecurity labs",
]

# Article seeds for the seo subcommand, Security+ first (the wedge
# track), then the CISSP capstone terms.
SEO_SEED_KEYWORDS = [
    "security+ practice questions",
    "security+ study guide",
    "how to pass security+",
    "security+ flashcards",
    "security+ exam cost",
    "cissp study guide",
    "cissp practice questions",
    "free cybersecurity labs online",
]
SEO_NICHE = "CISSP and CompTIA Security+ exam preparation"

BANNER = ("> **NOTHING WAS PUBLISHED AND NO MARKETING MONEY WAS SPENT.** Experiments "
          "run only after you approve them at /approvals; every draft waits for you "
          "to post it.")


# -- state -----------------------------------------------------------------

def default_state_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "road2cissp_growth.json"


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}
    state.setdefault("last_run", {})
    state.setdefault("last_attempt", {})
    state.setdefault("metrics", [])
    state.setdefault("experiments", {})
    state.setdefault("strategy_history", [])
    state.setdefault("spend_usd", 0.0)
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_due(state: dict, job: str, now: datetime, force: set[str]) -> bool:
    if job in force or "all" in force:
        return True
    last = _ts(state["last_run"].get(job))
    attempt = _ts(state["last_attempt"].get(job))
    if attempt and (last is None or attempt > last) and now - attempt < RETRY_AFTER:
        return False  # failed recently — back off
    return last is None or now - last >= CADENCE[job]


# -- helpers ---------------------------------------------------------------

def _business_id(rt) -> str:
    biz = next((b for b in rt.businesses.list() if b.name == BUSINESS_NAME), None)
    if biz is None:
        biz = rt.businesses.create(BUSINESS_NAME, "education_website",
                                   domain="road2cissp.com")
    return biz.id


def record_revenue(rt, business_id: str, amount_usd: float,
                   note: str = ""):
    """Book owner-reported Stripe subscription revenue. The entry names
    its source so the books never pretend to be an API sync."""
    if amount_usd <= 0:
        raise ValueError("revenue amount must be positive")
    suffix = f": {note}" if note else ""
    return rt.ledger.record(
        business_id=business_id, kind="revenue", amount_usd=amount_usd,
        description=f"Stripe subscription revenue — owner-reported{suffix}")


def record_external_cost(rt, business_id: str, amount_usd: float,
                         label: str):
    """Book a real-world cost (domain, hosting) against the business."""
    if amount_usd <= 0:
        raise ValueError("cost amount must be positive")
    if not label:
        raise ValueError("a label naming the cost is required")
    return rt.ledger.record(
        business_id=business_id, kind="external_cost",
        amount_usd=-amount_usd, description=label)


def print_pnl(rt, biz) -> None:
    pnl = rt.ledger.pnl(biz.id)
    status = getattr(biz, "status", "")
    state = getattr(status, "value", status)
    print(f"{biz.name} [{state}]")
    print(f"  revenue ${pnl.revenue:.2f} · costs "
          f"${pnl.total_costs:.2f} · net ${pnl.net_profit:.2f}")


def _dispatch(rt, biz_id, agent_type, action, inputs, job, criteria):
    usd, tokens = BUDGETS[job]
    task = rt.orchestrator.submit(agent_type, inputs={"action": action, **inputs},
                                  business_id=biz_id, budget_usd=usd,
                                  budget_tokens=tokens, acceptance_criteria=criteria)
    run = rt.orchestrator.dispatch(task.id)
    return run


def _slug_file(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or "draft"


def sync_experiments(state: dict, approvals, now: datetime) -> list[str]:
    """Apply owner decisions and end experiments whose duration elapsed."""
    events = []
    for slug, exp in state["experiments"].items():
        if exp["status"] in ("proposed", "superseded") and exp.get("approval_id"):
            a = approvals.get(exp["approval_id"])
            if a is None:
                continue
            if a.status == ApprovalStatus.APPROVED:
                exp["status"] = "active"
                exp["started_at"] = now.isoformat()
                exp["ends_at"] = (now + timedelta(days=exp["duration_days"])).isoformat()
                exp["metrics_at_start"] = state["metrics"][-1] if state["metrics"] else None
                events.append(f"experiment approved → active: {slug}")
            elif a.status == ApprovalStatus.REJECTED:
                exp["status"] = "rejected"
                exp["outcome"] = exp.get("outcome") or f"owner rejected: {a.note or 'no note'}"
                events.append(f"experiment rejected: {slug}")
        elif exp["status"] == "active" and (_ts(exp.get("ends_at")) or now) <= now:
            exp["status"] = "review"
            exp["metrics_at_end"] = state["metrics"][-1] if state["metrics"] else None
            events.append(f"experiment finished, awaiting outcome: {slug}")
    return events


def _past_experiments(state: dict) -> list[dict]:
    keep = ("slug", "name", "channel", "hypothesis", "kpi", "target", "status",
            "outcome", "metrics_at_start", "metrics_at_end")
    return [{k: e.get(k) for k in keep if e.get(k) is not None}
            for e in state["experiments"].values()
            if e["status"] in ("active", "review", "completed", "rejected")][-12:]


# -- jobs ------------------------------------------------------------------

def _job_audit(rt, biz_id, state, now):
    run = _dispatch(rt, biz_id, "marketing", "site_audit", {"site_url": SITE_URL},
                    "audit", ["site audit crawled pages with severity findings"])
    if run.status != TaskStatus.COMPLETED:
        raise RuntimeError(run.error)
    out = run.output
    state["audit"] = {"at": now.isoformat(), "summary": out["summary"],
                      "pages_checked": out["pages_checked"],
                      "severity_counts": out["severity_counts"],
                      "findings": out.get("findings", [])}
    return run, out["summary"]


def _job_keywords(rt, biz_id, state, now):
    run = _dispatch(rt, biz_id, "marketing", "seo_keyword_map",
                    {"niche": "CISSP exam preparation", "seeds": GROWTH_KEYWORDS},
                    "keywords", ["no invented numbers — gaps marked 'unavailable'"])
    if run.status != TaskStatus.COMPLETED:
        raise RuntimeError(run.error)
    state["keywords"] = {"at": now.isoformat(), "items": run.output["keywords"]}
    live = run.output["provenance"]["keywords_with_data"]
    return run, f"{len(run.output['keywords'])} keywords ({live} with live trend data)"


def _job_metrics(state, now):
    from core.site_metrics import MetricsUnavailable, PlausibleClient

    if not PlausibleClient.configured():
        return None
    try:
        snap = PlausibleClient().snapshot("7d")
    except MetricsUnavailable as exc:
        raise RuntimeError(str(exc)) from exc
    snap["at"] = now.isoformat()
    state["metrics"] = (state["metrics"] + [snap])[-120:]
    return f"7d visitors {snap.get('visitors')} · pageviews {snap.get('pageviews')}"


def _job_strategy(rt, biz_id, state, now, approvals):
    prev = state.get("strategy") or {}
    inputs = {
        "profile": SITE_PROFILE,
        "audit_findings": [{k: f[k] for k in ("severity", "issue", "fix")}
                           for f in (state.get("audit") or {}).get("findings", [])],
        "keywords": (state.get("keywords") or {}).get("items", []),
        "metrics": [{k: v for k, v in m.items() if k not in ("top_pages",)}
                    for m in state["metrics"][-8:]],
        "past_experiments": _past_experiments(state),
        "previous_summary": prev.get("summary"),
        "max_experiments": 4,
    }
    run = _dispatch(rt, biz_id, "marketing", "growth_strategy", inputs, "strategy",
                    ["experiments each with hypothesis, kpi, target and stop rule",
                     "ranked channel bets"])
    if run.status != TaskStatus.COMPLETED:
        raise RuntimeError(run.error)
    strategy = run.output["strategy"]
    strategy["at"] = now.isoformat()
    strategy["cycle"] = int(prev.get("cycle", 0)) + 1
    state["strategy"] = strategy
    state["strategy_history"] = (state["strategy_history"] + [
        {"at": strategy["at"], "cycle": strategy["cycle"],
         "summary": strategy.get("summary", "")}])[-12:]

    proposed_now = set()
    for exp in strategy["experiments"]:
        slug = exp["slug"]
        proposed_now.add(slug)
        existing = state["experiments"].get(slug)
        if existing:
            # Never ask twice: in flight, or already decided by the owner.
            if existing["status"] == "superseded":
                existing["status"] = "proposed"  # its approval is still queued
            continue
        approval = approvals.request(
            action="marketing_experiment",
            details={"business_name": BUSINESS_NAME, "site_url": SITE_URL,
                     "slug": slug, "name": exp["name"], "channel": exp.get("channel"),
                     "hypothesis": exp.get("hypothesis"), "kpi": exp.get("kpi"),
                     "target": exp.get("target"), "duration_days": exp["duration_days"],
                     "budget_usd": exp["budget_usd"], "stop_rule": exp.get("stop_rule"),
                     "actions": exp.get("actions", []),
                     "report_name": report_name(now)},
            amount_usd=exp["budget_usd"], business_id=biz_id,
            requested_by="road2cissp_growth")
        state["experiments"][slug] = {**exp, "status": "proposed",
                                      "approval_id": approval.id,
                                      "proposed_at": now.isoformat(),
                                      "cycle": strategy["cycle"]}
    for slug, exp in state["experiments"].items():
        if exp["status"] == "proposed" and slug not in proposed_now:
            exp["status"] = "superseded"  # still approvable; owner's call wins
    n = len(strategy["experiments"])
    return run, f"strategy cycle {strategy['cycle']}: {n} experiments proposed for approval"


def _job_content(rt, biz_id, exp, drafts_root: Path, now):
    run = _dispatch(rt, biz_id, "creative", "content_pieces",
                    {"experiment": exp, "profile": SITE_PROFILE}, "content",
                    ["content drafts stored as creative assets"])
    if run.status != TaskStatus.COMPLETED:
        raise RuntimeError(run.error)
    folder = drafts_root / exp["slug"]
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, p in enumerate(run.output["pieces"], 1):
        label = p.get("title") or p.get("subject") or p.get("venue") or p.get("platform")
        path = folder / f"{now:%Y%m%d}-{p['type']}-{i:02d}-{_slug_file(label)}.md"
        path.write_text(render_draft(p, exp), encoding="utf-8")
        paths.append(str(path))
    return run, paths


def render_draft(p: dict, exp: dict) -> str:
    meta = {k: v for k, v in p.items()
            if k not in ("body_markdown", "text", "body", "asset_id") and v}
    head = "\n".join(f"{k}: {v}" for k, v in meta.items())
    body = p.get("body_markdown") or p.get("text") or p.get("body") or ""
    return (f"<!-- DRAFT — not published. Experiment: {exp.get('name')} "
            f"({exp.get('slug')}) -->\n---\n{head}\n---\n\n{body}\n")


def build_today_plan(state_path: Path | None = None,
                     now: datetime | None = None) -> tuple[dict, dict]:
    """Build today's daily plan from the growth state. Pure read + build:
    no agent is dispatched, no money is spent, nothing is published.
    Returns (plan, state). Callers persist with daily_plan.record_plan."""
    from ecosystem import daily_plan

    now = now or datetime.now(timezone.utc)
    state_path = state_path or default_state_path()
    state = load_state(state_path)
    plan = daily_plan.build_daily_plan(
        business_name=BUSINESS_NAME, site_url=SITE_URL, state=state,
        now=now, cadence=CADENCE, retry_after=RETRY_AFTER)
    return plan, state


def _parse_plan_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        day = datetime.fromisoformat(value).date()
    except ValueError:
        raise SystemExit(f"bad --date {value!r}: use YYYY-MM-DD")
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)


# -- cycle -----------------------------------------------------------------

def report_name(now: datetime) -> str:
    return f"road2cissp_growth_{now:%Y%m%d}.md"


def run_growth_cycle(rt, state_path: Path | None = None, reports_root: Path | None = None,
                     now: datetime | None = None, force: set[str] | None = None,
                     llm_enabled: bool | None = None) -> dict:
    """Run every due job once. Never raises for a job failure — failures are
    reported in the summary and retried after RETRY_AFTER."""
    from core.llm import LLMGateway
    from dashboard.reports import reports_dir

    now = now or datetime.now(timezone.utc)
    force = force or set()
    state_path = state_path or default_state_path()
    reports_root = reports_root or reports_dir()
    llm_on = LLMGateway.enabled() if llm_enabled is None else llm_enabled
    state = load_state(state_path)
    biz_id = _business_id(rt)
    summary = {"ran": [], "errors": [], "events": [], "skipped": [], "spend_usd": 0.0,
               "report": None}

    summary["events"] += sync_experiments(state, rt.approvals, now)

    def attempt(job, fn):
        state["last_attempt"][job] = now.isoformat()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - one job must not stop the rest
            summary["errors"].append(f"{job}: {exc}")
            return
        state["last_run"][job] = now.isoformat()
        if result is None:
            return
        run, note = result if isinstance(result, tuple) else (None, result)
        if run is not None:
            summary["spend_usd"] += run.cost_usd or 0.0
        summary["ran"].append(f"{job}: {note}")

    if is_due(state, "audit", now, force):
        attempt("audit", lambda: _job_audit(rt, biz_id, state, now))
    if is_due(state, "keywords", now, force):
        attempt("keywords", lambda: _job_keywords(rt, biz_id, state, now))
    if is_due(state, "metrics", now, force):
        attempt("metrics", lambda: _job_metrics(state, now))
    if is_due(state, "strategy", now, force):
        if llm_on:
            attempt("strategy", lambda: _job_strategy(rt, biz_id, state, now, rt.approvals))
        else:
            summary["skipped"].append("strategy: needs LLM mode (set an API key)")

    drafts_root = reports_root / "road2cissp_drafts"
    for slug, exp in state["experiments"].items():
        if exp["status"] != "active" or exp.get("drafts") is not None:
            continue
        if not exp.get("content_needed"):
            exp["drafts"] = []
            continue
        if not llm_on:
            summary["skipped"].append(f"content for {slug}: needs LLM mode")
            continue
        key = f"content:{slug}"
        last = _ts(state["last_attempt"].get(key))
        if last and now - last < RETRY_AFTER and "content" not in force:
            continue
        state["last_attempt"][key] = now.isoformat()
        try:
            run, paths = _job_content(rt, biz_id, exp, drafts_root, now)
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"content {slug}: {exc}")
            continue
        exp["drafts"] = paths
        summary["spend_usd"] += run.cost_usd or 0.0
        summary["ran"].append(f"content: {len(paths)} drafts for {slug}")

    state["spend_usd"] = round(state["spend_usd"] + summary["spend_usd"], 6)
    if summary["spend_usd"]:
        rt.ledger.record(business_id=biz_id, kind="ai_spend",
                         amount_usd=-summary["spend_usd"],
                         description="road2cissp growth program")
    if summary["ran"] or summary["events"]:
        path = reports_root / report_name(now)
        write_report(path, state, now)
        summary["report"] = str(path)
        rt.audit.record(agent_type="road2cissp_growth", event="growth_cycle",
                        business_id=biz_id, result=summary)
    save_state(state_path, state)
    return summary


# -- report ----------------------------------------------------------------

# -- seo article run ---------------------------------------------------------


def run_seo(rt, state_path: Path | None = None, reports_root: Path | None = None,
            count: int = 2, word_target: int = 1500,
            now: datetime | None = None) -> dict:
    """One gated SEO article run against the live site (Farelune pattern):
    crawl the sitemap inventory, map keywords, write articles that must
    pass the deterministic quality gate, export them as drafts. Real
    per-step LLM costs are posted to the ledger as they happen. Drafts
    are NEVER published. Returns a summary dict; never raises for a
    step failure."""
    from core.seo_content import fetch_inventory, slugify, \
        suggest_internal_links
    from core.seo_publish import FileExportPublisher
    from dashboard.reports import reports_dir

    now = now or datetime.now(timezone.utc)
    state_path = state_path or default_state_path()
    reports_root = reports_root or reports_dir()
    state = load_state(state_path)
    biz_id = _business_id(rt)
    summary = {"ran": [], "errors": [], "spend_usd": 0.0, "articles": [],
               "report": None}

    def booked(run, label):
        if run is not None and run.cost_usd:
            summary["spend_usd"] += run.cost_usd
            rt.ledger.record(business_id=biz_id, kind="ai_spend",
                             amount_usd=-run.cost_usd,
                             description=f"road2cissp seo: {label}")

    try:
        inventory = fetch_inventory(SITE_URL)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        summary["errors"].append(f"inventory: {exc}")
        return summary
    summary["ran"].append(f"inventory: {len(inventory.pages)} live pages")

    run = _dispatch(rt, biz_id, "marketing", "seo_keyword_map",
                    {"niche": SEO_NICHE, "seeds": SEO_SEED_KEYWORDS},
                    "seo_keywords",
                    ["at least 1 keyword returned",
                     "no invented numbers — gaps marked 'unavailable' "
                     "explicitly"])
    booked(run, "seo_keyword_map")
    if run.status != TaskStatus.COMPLETED:
        summary["errors"].append(f"keywords: {run.error}")
        return summary

    drafted = state.setdefault("seo_articles", {})
    scored = sorted(
        run.output["keywords"],
        key=lambda k: (k.get("avg_12mo") is None, -(k.get("avg_12mo") or 0)))
    chosen = [k for k in scored
              if slugify(k["keyword"]) not in drafted][: max(1, count)]
    if not chosen:
        summary["ran"].append("seo: every mapped keyword already drafted")
        save_state(state_path, state)
        return summary

    facts = [SITE_PROFILE["what"], SITE_PROFILE["pricing"],
             *SITE_PROFILE["features"]]
    publisher = FileExportPublisher(reports_root / "seo" / "road2cissp_com")
    for entry_kw in chosen:
        keyword = entry_kw["keyword"]
        candidates = suggest_internal_links(keyword, inventory, limit=8)
        if len(candidates) < 3:
            have = {c["url"] for c in candidates}
            for page in inventory.pages:
                if len(candidates) >= 3:
                    break
                if page.url not in have:
                    candidates.append({"url": page.url, "title": page.title})
        run = _dispatch(
            rt, biz_id, "creative", "seo_article",
            {"keyword": keyword, "niche": SEO_NICHE, "site_url": SITE_URL,
             "facts": facts, "link_candidates": candidates,
             "inventory_urls": sorted(inventory.urls),
             "word_target": word_target},
            "seo_article",
            ["article package has title, meta_description, slug, "
             "body_markdown, faq, internal_links",
             "internal links only from the live site inventory"])
        booked(run, f"seo_article {keyword!r}")
        if run.status != TaskStatus.COMPLETED:
            summary["errors"].append(f"article {keyword!r}: {run.error}")
            continue
        pkg = run.output["package"]
        result = publisher.publish(pkg, SITE_URL)
        drafted[pkg["slug"]] = {"keyword": keyword, "at": now.isoformat(),
                                "path": result["markdown"]}
        summary["articles"].append(pkg["slug"])
        summary["ran"].append(
            f"article drafted: {pkg['slug']} → {result['markdown']}")
    save_state(state_path, state)

    if summary["articles"] or summary["errors"]:
        path = reports_root / f"road2cissp_seo_{now:%Y%m%d}.md"
        lines = [f"# Road to CISSP — SEO article run ({now:%Y-%m-%d})", "",
                 "> **NOTHING WAS PUBLISHED.** Every article below is a "
                 "draft waiting for the owner's review.", ""]
        lines += [f"- {line}" for line in summary["ran"]]
        lines += [f"- ERROR: {line}" for line in summary["errors"]]
        lines += ["", f"Real AI spend this run: ${summary['spend_usd']:.4f}"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        summary["report"] = str(path)
    return summary


# -- community listening -----------------------------------------------------

def _render_community_draft(thread: dict, reply: dict) -> str:
    return (
        f"<!-- DRAFT REPLY — not posted. Thread: {thread.get('url', '')} -->\n"
        f"---\n"
        f"thread: {thread.get('url', '')}\n"
        f"venue: {thread.get('venue', '')}\n"
        f"title: {thread.get('title', '')}\n"
        f"intent: {thread.get('intent_score', '')}/100 "
        f"({thread.get('intent', '')})\n"
        f"reasons: {'; '.join(thread.get('reasons', []))}\n"
        f"---\n\n{reply.get('text', '')}\n"
    )


def run_listen(rt, state_path: Path | None = None,
               reports_root: Path | None = None,
               now: datetime | None = None, limit: int = 5,
               llm_enabled: bool | None = None) -> dict:
    """One community-listening run: search Reddit for live threads about
    the business's keywords, score them, draft replies to the best
    ones. Drafts only — nothing is posted, ever. The search itself
    needs no LLM; drafting does. Returns a summary dict; never raises
    for a source failure (an unavailable source is reported, not
    papered over)."""
    from core.community_listening import REPLY_THRESHOLD, listen_reddit
    from core.llm import LLMGateway
    from dashboard.reports import reports_dir

    now = now or datetime.now(timezone.utc)
    state_path = state_path or default_state_path()
    reports_root = reports_root or reports_dir()
    state = load_state(state_path)
    biz_id = _business_id(rt)
    llm_on = LLMGateway.enabled() if llm_enabled is None else llm_enabled
    summary: dict = {"ran": [], "errors": [], "spend_usd": 0.0,
                     "drafted": [], "dropped": [], "sources": {},
                     "report": None}
    subreddits = [c for c in SITE_PROFILE["communities"]
                  if c.startswith("r/")]
    result = listen_reddit(subreddits, LISTEN_KEYWORDS, now=now)
    summary["sources"] = result["sources"]
    reddit = result["sources"]["reddit"]
    if reddit["status"] != "ok":
        summary["errors"].append(f"reddit: {reddit['reason']}")
    summary["ran"].append(
        f"reddit: {reddit.get('queries', 0)} searches, "
        f"{reddit.get('raw_found', 0)} threads found, "
        f"{len(result['threads'])} kept after filtering")
    for source in ("x", "threads"):
        summary["ran"].append(
            f"{source}: unavailable — {result['sources'][source]['reason']}")

    store = state.setdefault("community_threads", {})
    for thread in result["threads"]:
        store.setdefault(thread["id"], {**thread, "status": "found"})
    candidates = [t for t in result["threads"]
                  if t["intent_score"] >= REPLY_THRESHOLD
                  and store[t["id"]]["status"] == "found"][: max(1, limit)]
    summary["ran"].append(
        f"{len(candidates)} threads worth a reply "
        f"(intent >= {REPLY_THRESHOLD})")

    if candidates and not llm_on:
        summary["ran"].append(
            "drafting skipped: needs LLM mode (set an API key); "
            "threads are recorded as found")
    elif candidates:
        facts = [SITE_PROFILE["what"], SITE_PROFILE["pricing"],
                 *SITE_PROFILE["features"]]
        run = _dispatch(
            rt, biz_id, "creative", "community_replies",
            {"threads": candidates, "profile": SITE_PROFILE,
             "site_url": SITE_URL, "facts": facts},
            "community",
            ["reply drafts stored as creative assets",
             "nothing posted"])
        if run.cost_usd:
            summary["spend_usd"] += run.cost_usd
            rt.ledger.record(business_id=biz_id, kind="ai_spend",
                             amount_usd=-run.cost_usd,
                             description="road2cissp community listening")
        if run.status != TaskStatus.COMPLETED:
            summary["errors"].append(f"drafting: {run.error}")
        else:
            drafts_root = reports_root / "community_drafts"
            for reply in run.output.get("replies", []):
                thread = store[reply["thread_id"]]
                folder = drafts_root
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{reply['thread_id']}.md"
                path.write_text(
                    _render_community_draft(thread, reply), encoding="utf-8")
                thread["status"] = "drafted"
                thread["draft_path"] = str(path)
                thread["drafted_at"] = now.isoformat()
                summary["drafted"].append(reply["thread_id"])
            for drop in run.output.get("dropped", []):
                thread = store[drop["thread_id"]]
                thread["status"] = "dropped"
                thread["drop_errors"] = drop["errors"]
                summary["dropped"].append(
                    f"{drop['thread_id']}: {'; '.join(drop['errors'])}")
            summary["ran"].append(
                f"drafted {len(summary['drafted'])} replies, "
                f"dropped {len(summary['dropped'])} by the 90/10 gate")

    state["community_last_listen"] = {
        "at": now.isoformat(), "sources": summary["sources"],
        "found": len(result["threads"]), "drafted": len(summary["drafted"])}
    state["spend_usd"] = round(
        state["spend_usd"] + summary["spend_usd"], 6)
    save_state(state_path, state)

    path = reports_root / f"community_{now:%Y%m%d}.md"
    lines = [f"# Road to CISSP — community listening ({now:%Y-%m-%d})", "",
             "> **NOTHING WAS POSTED.** Every reply below is a draft "
             "waiting for the owner's review — post it by hand, then "
             "mark it done.", "",
             "## Sources", ""]
    for name, info in summary["sources"].items():
        lines.append(f"- **{name}**: {info['status']}"
                     + (f" — {info['reason']}" if info.get("reason") else ""))
    lines += ["", "## Found", ""]
    for thread in result["threads"][:10]:
        lines.append(f"- [{thread['intent_score']}/100 {thread['intent']}] "
                     f"{thread['venue']}: {thread['title']} "
                     f"({thread['url']}) — {'; '.join(thread['reasons'])}")
    if not result["threads"]:
        lines.append("- No threads kept after filtering.")
    lines += ["", "## Drafts", ""]
    lines += [f"- {tid}" for tid in summary["drafted"]] or ["- None."]
    if summary["dropped"]:
        lines += ["", "## Dropped by the 90/10 gate", ""]
        lines += [f"- {d}" for d in summary["dropped"]]
    lines += ["", f"Real AI spend this run: ${summary['spend_usd']:.4f}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary["report"] = str(path)
    return summary


def _cell(v) -> str:
    return str(v if v is not None else "—").replace("|", "/").replace("\n", " ")


def _line(x) -> str:
    if isinstance(x, dict):
        return " — ".join(_cell(v) for v in x.values() if v)
    return _cell(x)


def write_report(path: Path, state: dict, now: datetime) -> None:
    s = state.get("strategy") or {}
    audit = state.get("audit") or {}
    exps = state["experiments"]
    pending = [e for e in exps.values() if e["status"] in ("proposed", "superseded")]
    review = [e for e in exps.values() if e["status"] == "review"]
    out = [f"# Road to CISSP Growth — {SITE_URL}", "",
           f"_Generated {now.isoformat()} · strategy cycle {s.get('cycle', 0)}_", "",
           BANNER, "", "## Your move", ""]
    if pending:
        out.append(f"- **Approve or reject {len(pending)} experiment(s)** at /approvals: "
                   + ", ".join(e["name"] for e in pending))
    if review:
        out.append("- **Record outcomes** for finished experiments: "
                   + ", ".join(f"`outcome {e['slug']}`" for e in review))
    for t in s.get("this_week", []):
        out.append(f"- {_line(t)}")
    if not s:  # no strategy yet: the audit's critical fixes are the to-do list
        out += [f"- **Fix (critical):** {_cell(f['issue'])} → {_cell(f['fix'])}"
                for f in audit.get("findings", []) if f["severity"] == "critical"]
    drafts = [(e, d) for e in exps.values() for d in (e.get("drafts") or [])]
    if drafts:
        out.append(f"- **Review and post {len(drafts)} drafts** (listed below)")
    if out[-1] == "":
        out.append("- Nothing needs you right now.")

    if s:
        ns = s.get("north_star") or {}
        out += ["", "## Strategy", "", _cell(s.get("summary")), "",
                f"**North star:** {_cell(ns.get('metric'))} — now "
                f"{_cell(ns.get('current'))}, 90-day target {_cell(ns.get('target_90d'))}. "
                f"{_cell(ns.get('why'))}", "", "**Diagnosis**"]
        out += [f"- {_line(d)}" for d in s.get("diagnosis", [])]
        out += ["", "**Channel bets (ranked by ICE)**", "",
                "| Channel | Impact | Confidence | Ease | ICE | Why |",
                "|---------|--------|------------|------|-----|-----|"]
        out += [f"| {_cell(b.get('channel'))} | {b['impact']} | {b['confidence']} | "
                f"{b['ease']} | {b['ice']} | {_cell(b.get('rationale'))} |"
                for b in s.get("channel_bets", [])]
        if s.get("site_fixes"):
            out += ["", "**Site fixes**"]
            out += [f"- {_line(f)}" for f in s["site_fixes"]]

    out += ["", "## Experiments", "",
            "| Experiment | Channel | KPI → target | Days | Budget | Status | Outcome |",
            "|------------|---------|--------------|------|--------|--------|---------|"]
    out += [f"| {_cell(e['name'])} | {_cell(e.get('channel'))} | {_cell(e.get('kpi'))} → "
            f"{_cell(e.get('target'))} | {e['duration_days']} | ${e['budget_usd']:.0f} | "
            f"{e['status']} | {_cell(e.get('outcome'))} |" for e in exps.values()]
    if not exps:
        out.append("| none yet | | | | | | |")

    out += ["", "## Measurement", ""]
    if state["metrics"]:
        out += ["| When | Source | Visitors (7d) | Pageviews | Sign-ins | Note |",
                "|------|--------|---------------|-----------|----------|------|"]
        out += [f"| {_cell(m.get('at', '')[:10])} | {_cell(m.get('source'))} | "
                f"{_cell(m.get('visitors'))} | {_cell(m.get('pageviews'))} | "
                f"{_cell(m.get('signups'))} | {_cell(m.get('note'))} |"
                for m in state["metrics"][-10:]]
    else:
        out.append("No metrics yet. Install analytics (see audit) and set "
                   "`PLAUSIBLE_API_KEY`, or record numbers with `record-metrics`.")

    if audit:
        out += ["", f"## Site audit ({_cell(audit.get('at', '')[:10])})", "",
                _cell(audit.get("summary")), "",
                "| Severity | Area | Issue | Fix |", "|----------|------|-------|-----|"]
        out += [f"| {f['severity']} | {f['area']} | {_cell(f['issue'])} | {_cell(f['fix'])} |"
                for f in audit.get("findings", [])]

    kws = (state.get("keywords") or {}).get("items", [])
    if kws:
        out += ["", "## Keyword trends (Google Trends, 12 months)", "",
                "| Keyword | Avg interest | Trend |", "|---------|--------------|-------|"]
        out += [f"| {_cell(k['keyword'])} | {_cell(k.get('avg_12mo'))} | {_cell(k.get('trend'))} |"
                for k in sorted(kws, key=lambda k: -(k.get("avg_12mo") or -1))]

    if drafts:
        out += ["", "## Drafts awaiting your review", ""]
        out += [f"- `{Path(d).name}` — {e['name']}" for e, d in drafts]

    out += ["", f"_AI spend to date: ${state['spend_usd']:.4f}_", "", "---", BANNER, ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")


# -- CLI -------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Road to CISSP growth program")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="run every due job now")
    p_run.add_argument("--force", default="",
                       help="comma list of jobs to run regardless of cadence "
                            "(audit,keywords,metrics,strategy,content or all)")
    p_run.add_argument("--llm", dest="llm", action="store_true", default=None)
    p_run.add_argument("--no-llm", dest="llm", action="store_false")
    sub.add_parser("status", help="show experiments and last runs")
    p_dp = sub.add_parser("daily-plan",
                          help="today's ordered moves (builds the plan, "
                               "runs nothing)")
    p_dp.add_argument("--date", default=None,
                      help="plan date YYYY-MM-DD (default: today, UTC)")
    p_dpd = sub.add_parser("daily-plan-done",
                           help="mark one item on a daily plan done")
    p_dpd.add_argument("item_id")
    p_dpd.add_argument("--date", default=None,
                       help="plan date YYYY-MM-DD (default: today, UTC)")
    p_dps = sub.add_parser("daily-plan-skip",
                           help="mark one item on a daily plan skipped")
    p_dps.add_argument("item_id")
    p_dps.add_argument("--date", default=None,
                       help="plan date YYYY-MM-DD (default: today, UTC)")
    p_m = sub.add_parser("record-metrics", help="record numbers by hand")
    p_m.add_argument("--visitors", type=int)
    p_m.add_argument("--pageviews", type=int)
    p_m.add_argument("--signups", type=int)
    p_m.add_argument("--note", default="")
    p_o = sub.add_parser("outcome", help="record how a finished experiment went")
    p_o.add_argument("slug")
    g = p_o.add_mutually_exclusive_group(required=True)
    g.add_argument("--worked", metavar="NOTE")
    g.add_argument("--failed", metavar="NOTE")
    p_s = sub.add_parser("seo", help="run the gated SEO article pipeline once")
    p_s.add_argument("--count", type=int, default=2)
    p_s.add_argument("--word-target", type=int, default=1500)
    p_s.add_argument("--llm", dest="llm", action="store_true", default=None)
    p_s.add_argument("--no-llm", dest="llm", action="store_false")
    p_l = sub.add_parser("listen",
                         help="search Reddit for live threads and draft "
                              "replies (drafts only, nothing posted)")
    p_l.add_argument("--limit", type=int, default=5,
                     help="max replies to draft this run")
    p_l.add_argument("--llm", dest="llm", action="store_true", default=None)
    p_l.add_argument("--no-llm", dest="llm", action="store_false")
    p_r = sub.add_parser("record-revenue",
                         help="book Stripe subscription revenue (owner-reported)")
    p_r.add_argument("--amount", type=float, required=True)
    p_r.add_argument("--note", default="")
    p_c = sub.add_parser("record-cost", help="book a real-world cost")
    p_c.add_argument("--amount", type=float, required=True)
    p_c.add_argument("--label", required=True)
    sub.add_parser("pnl", help="print the business P&L from the ledger")
    args = parser.parse_args(argv)

    path = default_state_path()
    if args.cmd == "run":
        from ecosystem.runtime import build_runtime

        rt = build_runtime(use_llm=args.llm, approvals_persist=True)
        force = {f.strip() for f in args.force.split(",") if f.strip()}
        summary = run_growth_cycle(rt, force=force, llm_enabled=args.llm)
        for key in ("events", "ran", "skipped", "errors"):
            for line in summary[key]:
                print(f"{key}: {line}")
        print(f"spend: ${summary['spend_usd']:.4f}")
        if summary["report"]:
            print(f"report: {summary['report']}")
        return 1 if summary["errors"] and not summary["ran"] else 0

    if args.cmd == "seo":
        from core.llm import LLMGateway
        from ecosystem.runtime import build_runtime

        rt = build_runtime(use_llm=args.llm, approvals_persist=True)
        llm_on = LLMGateway.enabled() if args.llm is None else args.llm
        if not llm_on:
            print("seo: needs LLM mode (set an API key)")
            return 2
        summary = run_seo(rt, count=args.count,
                          word_target=args.word_target)
        for line in summary["ran"]:
            print(f"ran: {line}")
        for line in summary["errors"]:
            print(f"error: {line}")
        print(f"spend: ${summary['spend_usd']:.4f}")
        if summary["report"]:
            print(f"report: {summary['report']}")
        return 1 if summary["errors"] and not summary["articles"] else 0

    if args.cmd == "listen":
        from ecosystem.runtime import build_runtime

        rt = build_runtime(use_llm=args.llm, approvals_persist=True)
        summary = run_listen(rt, limit=args.limit, llm_enabled=args.llm)
        for line in summary["ran"]:
            print(f"ran: {line}")
        for line in summary["errors"]:
            print(f"error: {line}")
        for tid in summary["drafted"]:
            print(f"drafted: {tid}")
        print(f"spend: ${summary['spend_usd']:.4f}")
        if summary["report"]:
            print(f"report: {summary['report']}")
        return 1 if summary["errors"] and not summary["drafted"] else 0

    if args.cmd in ("daily-plan", "daily-plan-done", "daily-plan-skip"):
        from dashboard.reports import reports_dir
        from ecosystem import daily_plan

        now = _parse_plan_date(args.date)
        date = (now or datetime.now(timezone.utc)).date().isoformat()
        if args.cmd == "daily-plan":
            plan, state = build_today_plan(now=now)
            daily_plan.record_plan(state, plan)
            save_state(path, state)
            report_path = reports_dir() / daily_plan.report_name(plan["date"])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(daily_plan.render_markdown(plan),
                                   encoding="utf-8")
            print(daily_plan.render_text(plan), end="")
            print(f"report: {report_path}")
            return 0
        state = load_state(path)
        try:
            status = "done" if args.cmd == "daily-plan-done" else "skipped"
            # The item's source_key (from the plan for that date) is
            # what makes "done" sticky until its source changes.
            plan_for_date, _ = build_today_plan(now=now)
            source_key = next(
                (i.get("source_key") for i in plan_for_date["items"]
                 if i["id"] == args.item_id),
                None)
            daily_plan.mark_item(state, date, args.item_id, status,
                                 source_key=source_key)
        except ValueError as exc:
            print(str(exc))
            return 1
        if status == "done" and args.item_id.startswith("reply:"):
            # The owner posted that reply by hand: the thread leaves
            # the "drafted" pool so the daily plan never lists it again.
            thread = (state.get("community_threads") or {}).get(
                args.item_id[len("reply:"):])
            if thread is not None:
                thread["status"] = "done"
        save_state(path, state)
        print(f"{args.item_id}: {status} on {date}")
        return 0

    if args.cmd in ("record-revenue", "record-cost", "pnl"):
        from ecosystem.runtime import build_runtime

        rt = build_runtime(approvals_persist=True)
        biz_id = _business_id(rt)
        if args.cmd == "record-revenue":
            entry = record_revenue(rt, biz_id, args.amount, args.note)
            print(f"booked revenue ${entry.amount_usd:.2f}: "
                  f"{entry.description}")
        elif args.cmd == "record-cost":
            entry = record_external_cost(rt, biz_id, args.amount,
                                          args.label)
            print(f"booked cost ${-entry.amount_usd:.2f}: "
                  f"{entry.description}")
        print_pnl(rt, rt.businesses.get(biz_id))
        return 0

    state = load_state(path)
    if args.cmd == "status":
        for job in ("audit", "keywords", "metrics", "strategy"):
            print(f"{job:9} last run {state['last_run'].get(job, 'never')}")
        print(f"seo articles drafted: {len(state.get('seo_articles', {}))}")
        community = state.get("community_threads", {})
        if community:
            by_status: dict[str, int] = {}
            for t in community.values():
                by_status[t.get("status", "found")] = \
                    by_status.get(t.get("status", "found"), 0) + 1
            print("community threads: " + ", ".join(
                f"{n} {s}" for s, n in sorted(by_status.items())))
        for slug, e in state["experiments"].items():
            print(f"- [{e['status']}] {slug}: {e['name']}"
                  + (f" (ends {e['ends_at'][:10]})" if e.get("ends_at") else ""))
        print(f"AI spend to date: ${state['spend_usd']:.4f}")
        return 0
    if args.cmd == "record-metrics":
        entry = {"at": datetime.now(timezone.utc).isoformat(), "source": "manual",
                 "visitors": args.visitors, "pageviews": args.pageviews,
                 "signups": args.signups, "note": args.note or None}
        state["metrics"] = (state["metrics"] + [entry])[-120:]
        save_state(path, state)
        print("recorded:", {k: v for k, v in entry.items() if v is not None})
        return 0
    if args.cmd == "outcome":
        exp = state["experiments"].get(args.slug)
        if exp is None:
            print(f"no experiment {args.slug!r}; see `status`")
            return 1
        exp["status"] = "completed"
        exp["outcome"] = (f"worked: {args.worked}" if args.worked
                          else f"failed: {args.failed}")
        save_state(path, state)
        print(f"{args.slug}: {exp['outcome']}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
