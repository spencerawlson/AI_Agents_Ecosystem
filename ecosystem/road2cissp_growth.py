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
    python ecosystem/road2cissp_growth.py record-metrics --visitors 420 --signups 12
    python ecosystem/road2cissp_growth.py outcome <slug> --worked "r/cissp answers drove 60 visits"
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
    "pricing": "Free. Nothing is locked; sign-in (Google/GitHub OAuth) saves "
               "progress and unlocks the AI tutor.",
    "features": SITE_FEATURES + [
        "AI study tutor (hints, explanations, weak-spot analysis) for signed-in learners",
        "CISSP roadmap, learning paths and courses → modules → lessons",
        "Brilliant-style guided lessons mixing questions with hands-on puzzles",
    ],
    "public_pages": ["/", "/academy/roadmap", "/academy/paths", "/academy/courses",
                     "/academy/exam", "/library", "/lab", "/labs", "/practice"],
    "goal": "Grow organic traffic, sign-ins and returning learners.",
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
}

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

    state = load_state(path)
    if args.cmd == "status":
        for job in ("audit", "keywords", "metrics", "strategy"):
            print(f"{job:9} last run {state['last_run'].get(job, 'never')}")
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
