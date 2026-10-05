"""Marketing mission: put the marketing + creative agents to work on real
marketing for road2cissp.com (Spencer's Road to CISSP IT-training site).

Produces, in order:
  1. SEO keyword map backed by LIVE Google Trends data
  2. Positioning brief (audiences, value props, angles, channels)
  3. 30-day content calendar
  4. Social post drafts (stored as creative assets)

NOTHING IS PUBLISHED. All outputs are drafts written to a Markdown report
for human approval. All agent spend stays inside hard per-task budgets.

Usage (on the Fedora server):
    export OPENAI_API_KEY=...
    python3 ecosystem/marketing_mission.py --llm
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.models import BusinessStatus
from ecosystem.runtime import build_runtime
from orchestrator.models import TaskStatus

SEED_KEYWORDS = [
    "cissp practice questions",
    "cissp exam prep",
    "cissp study guide",
    "cissp practice test",
    "cissp domain 1 security and risk management",
    "cissp cheat sheet",
    "cissp vs security+",
    "how to pass cissp",
    "cissp flashcards",
    "cissp mind maps",
    "cissp 8 domains explained",
    "free cissp practice exam",
]

BANNER = (
    "> **NOTHING WAS PUBLISHED — all drafts below await human approval.** "
    "No ads were bought, no emails sent, no posts published."
)

# Real differentiators of road2cissp.com, fed to the positioning brief so
# its value props are grounded in the actual product (not generic).
SITE_FEATURES = [
    "1004+ flashcards mapped to the official CISSP exam domains",
    "Interactive hands-on labs: Cisco IOS shell, Python scripting, Linux terminal",
    "3D visual lab: explorable network topologies (OSI packet flow, load balancers)",
    "Timed 100-question CISSP practice exams with per-domain score reports",
    "Gamification: study streaks, XP, ranks and badges with spaced repetition",
]


def _require_llm_ready() -> None:
    """Fail fast with a precise diagnosis when the mission needs the LLM but
    the gateway can't actually run (missing litellm or no API key in env).

    Without this, the run prints "LLM ON", completes step 1, then dies at
    step 2 with a confusing "requires LLM mode" error.
    """
    import os

    from core.llm import LLMGateway

    if LLMGateway.enabled():
        return
    missing = []
    try:
        import litellm  # noqa: F401
    except ImportError:
        missing.append("litellm is not installed — run: pip install litellm")
    if not any(os.environ.get(k) for k in (
            "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")):
        missing.append("no API key in environment — export OPENAI_API_KEY=...")
    raise SystemExit("LLM unavailable: " + "; ".join(missing))


def run_step(orch, agent_type, action, inputs, business_id,
             budget_usd, budget_tokens, criteria):
    """Submit + dispatch one mission step. Returns (ok, output_or_error, run)."""
    task = orch.submit(
        agent_type,
        inputs={"action": action, **inputs},
        business_id=business_id,
        budget_usd=budget_usd,
        budget_tokens=budget_tokens,
        acceptance_criteria=criteria,
    )
    run = orch.dispatch(task.id)
    if run.status != TaskStatus.COMPLETED:
        return False, f"{agent_type}/{action}: {run.error}", run
    return True, run.output, run


def write_report(path: Path, ctx: dict) -> None:
    kw_rows = "\n".join(
        f"| {k['keyword']} | "
        f"{k['avg_12mo'] if k['avg_12mo'] is not None else 'n/a'} | "
        f"{k['trend']} |"
        for k in ctx["keywords"]
    )
    brief = ctx.get("brief", {})
    cal_rows = "\n".join(
        f"| {e.get('day')} | {e.get('theme')} | {e.get('format')} | "
        f"{e.get('keyword')} | {e.get('working_title')} |"
        for e in ctx.get("calendar", [])
    )
    draft_rows = "\n".join(
        f"### {d.get('platform', '?')} — {d.get('hook', '')}\n\n"
        f"{d.get('text', '')}\n"
        for d in ctx.get("drafts", [])
    )
    def _fmt(x):
        # The LLM sometimes returns dicts (e.g. {"name": ..., "pain_point": ...});
        # render those as readable lines instead of raw dict reprs.
        if isinstance(x, dict):
            name = x.get("name") or x.get("title") or ""
            desc = (x.get("pain_point") or x.get("description")
                    or x.get("text") or "")
            if name and desc:
                return f"- **{name}** — {desc}"
            return f"- {name or desc}"
        return f"- {x}"

    audiences = "\n".join(_fmt(a) for a in brief.get("audiences", []))
    props = "\n".join(_fmt(p) for p in brief.get("value_props", []))
    angles = "\n".join(_fmt(a) for a in brief.get("content_angles", []))
    channels = "\n".join(_fmt(c) for c in brief.get("channels_ranked", []))

    report = f"""# Marketing Mission — {ctx['site_url']}

_Generated {ctx['generated_at']} · niche: {ctx['niche']}_

{BANNER}

## Spend

Total AI spend this mission: **${ctx['total_spend_usd']:.4f}**
({ctx['total_tokens']} tokens across {ctx['steps_completed']} agent steps)

## 1. SEO keyword map (live Google Trends, US, 12-month)

| Keyword | Avg interest | Trend |
|---------|--------------|-------|
{kw_rows}

## 2. Positioning brief

**Audiences**
{audiences}

**Value props**
{props}

**Content angles**
{angles}

**Channels (ranked)**
{channels}

## 3. {ctx['days']}-day content calendar

| Day | Theme | Format | Keyword | Working title |
|-----|-------|--------|---------|---------------|
{cal_rows}

## 4. Social drafts (for approval — not published)

{draft_rows}

---
{BANNER}
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Marketing mission for road2cissp.com")
    parser.add_argument("--llm", dest="llm", action="store_true", default=None,
                        help="force LLM mode (needs OPENAI_API_KEY in env)")
    parser.add_argument("--no-llm", dest="llm", action="store_false",
                        help="force heuristic mode (LLM steps will fail)")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--site-url", default="https://road2cissp.com")
    parser.add_argument("--niche", default="CISSP exam preparation")
    args = parser.parse_args(argv)

    rt = build_runtime(use_llm=args.llm, approvals_persist=True)
    orch = rt.orchestrator

    # The mission's steps 2-4 need the LLM. Fail fast with a clear diagnosis
    # instead of dying at step 2. (--no-llm explicitly opts into the failure.)
    if args.llm is not False:
        _require_llm_ready()

    biz = rt.businesses.create(
        name="Road to CISSP",
        business_type="education_website",
        domain="road2cissp.com",
    )
    rt.businesses.transition(biz.id, BusinessStatus.RESEARCHING)
    print(f"business {biz.id} ({biz.name}) -> researching", flush=True)

    ctx: dict = {
        "site_url": args.site_url,
        "niche": args.niche,
        "days": args.days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "keywords": [],
        "brief": {},
        "calendar": [],
        "drafts": [],
        "total_spend_usd": 0.0,
        "total_tokens": 0,
        "steps_completed": 0,
    }

    steps = [
        ("marketing", "seo_keyword_map",
         {"niche": args.niche, "seeds": SEED_KEYWORDS}, 1.0, 4000,
         ["at least 9 keywords returned",
          "each keyword has avg_12mo and a trend value or 'unavailable'",
          "no invented numbers — gaps marked 'unavailable' explicitly"]),
        ("marketing", "positioning_brief",
         {"niche": args.niche, "site_url": args.site_url,
          "features": SITE_FEATURES}, 2.0, 6000,
         ["audiences, value_props, content_angles, channels_ranked present"]),
        ("marketing", "content_calendar",
         {"niche": args.niche, "days": args.days}, 3.0, 12000,
         [f"{args.days} calendar entries",
          "each entry has day, theme, format, keyword, working_title"]),
    ]

    outputs: dict = {}
    for agent_type, action, inputs, usd, tokens, criteria in steps:
        if action == "content_calendar":
            inputs = {**inputs, "keyword_map": outputs.get("seo_keyword_map", {})}
        ok, result, run = run_step(
            orch, agent_type, action, inputs, biz.id, usd, tokens, criteria)
        ctx["total_spend_usd"] += run.cost_usd or 0.0
        ctx["total_tokens"] += run.tokens_used or 0
        if not ok:
            print(f"MISSION STOPPED: {result}", flush=True)
            report_path = Path("reports") / (
                f"marketing_mission_{datetime.now(timezone.utc):%Y%m%d}.md")
            ctx["error"] = result
            write_report(report_path, ctx)
            print(f"partial report: {report_path}", flush=True)
            return 1
        outputs[action] = result
        ctx["steps_completed"] += 1
        print(f"ok: {agent_type}/{action} "
              f"(${run.cost_usd:.4f}, {run.tokens_used} tokens)", flush=True)

    ctx["keywords"] = outputs["seo_keyword_map"]["keywords"]
    ctx["brief"] = outputs["positioning_brief"]["brief"]

    calendar = outputs["content_calendar"]["calendar"]
    ctx["calendar"] = calendar
    themes = [e.get("working_title", "") for e in calendar[:10] if e.get("working_title")]

    ok, result, run = run_step(
        orch, "creative", "social_drafts",
        {"niche": args.niche, "themes": themes, "count": 10},
        biz.id, 2.0, 8000,
        ["10 drafts returned", "each draft has platform, text, hook"])
    ctx["total_spend_usd"] += run.cost_usd or 0.0
    ctx["total_tokens"] += run.tokens_used or 0
    if not ok:
        print(f"MISSION STOPPED: {result}", flush=True)
        return 1
    ctx["drafts"] = result["drafts"]
    ctx["steps_completed"] += 1
    print(f"ok: creative/social_drafts "
          f"(${run.cost_usd:.4f}, {run.tokens_used} tokens)", flush=True)

    report_path = Path("reports") / (
        f"marketing_mission_{datetime.now(timezone.utc):%Y%m%d}.md")
    write_report(report_path, ctx)
    print(f"\n{BANNER}", flush=True)
    print(f"report: {report_path}", flush=True)
    print(f"total AI spend: ${ctx['total_spend_usd']:.4f} "
          f"({ctx['total_tokens']} tokens)", flush=True)
    request_report_approval(rt, biz, report_path, ctx)
    return 0


def request_report_approval(rt, biz, report_path: Path, ctx: dict):
    """Register GUI approval for the finished mission report.

    Approving means "a human signed off on the drafts" — it never
    publishes anything on its own; publishing stays a separate,
    explicit human action.
    """
    approval = rt.approvals.request(
        action="mission_report_approval",
        details={
            "mission": "marketing_mission",
            "business_name": biz.name,
            "site_url": ctx["site_url"],
            "report_path": str(report_path),
            "report_name": report_path.name,
            "drafts_count": len(ctx["drafts"]),
            "total_spend_usd": round(ctx["total_spend_usd"], 4),
            "total_tokens": ctx["total_tokens"],
            "steps_completed": ctx["steps_completed"],
        },
        amount_usd=round(ctx["total_spend_usd"], 4),
        business_id=biz.id,
        requested_by="marketing_mission",
    )
    print(f"approval requested: {approval.id} — review it at /approvals",
          flush=True)
    return approval


if __name__ == "__main__":
    raise SystemExit(main())
