"""Daily plan: one ordered list of today's marketing moves.

This turns the weekly growth cycle (see businesses/road2cissp/growth.py)
into the thing HeyCatch sells as a daily plan: wake up, see the moves
ranked by channel and priority, do them in order.

    P0 site fixes + critical audit findings
    → owner decisions waiting (approve experiments, record outcomes)
    → drafted community replies (threads go stale — answer them first)
    → jobs due today (audit, keywords, metrics, strategy)
    → drafts for active experiments
    → this week's strategy tasks
    → next gated SEO article
    → measurement gap (no metrics yet)

Rules, all deliberate:

- Pure and deterministic. The builder reads only the growth state dict
  and the clock: no LLM, no network, no spend, no publishing. The same
  state + date always builds the same plan, so it is cheap to test and
  safe to run every morning.
- Nothing is executed. Every item names the exact command (or dashboard
  page) that does the work; the owner runs it. Building a plan never
  dispatches an agent, never books spend, never publishes anything.
- Carry-over, not amnesia. An item on yesterday's plan that was not
  marked done or skipped comes back today, flagged ``carried_over``,
  ahead of same-tier new items.
- Done is sticky, skip is for today. Marking an item done hides it
  until its source changes (a new strategy cycle, a new audit, a new
  experiment status, a new drafted article) — the work is finished,
  so it must not nag you again just because the old strategy text is
  still in the state file. Skipping only defers it to tomorrow.
  Due jobs (``job:*``) are the exception: they are never sticky-done.
  A job reappears whenever its cadence says it is due again, because
  marking a job done without running it would be a lie — run it, and
  its ``last_run`` moves.
- At most MAX_ITEMS items. A daily list that never ends is not a plan.
  Anything beyond the cap is counted in ``truncated`` and stays in the
  weekly report, where it belongs.
- Item ids are stable for the day (``approve:<slug>``,
  ``outcome:<slug>``, ``job:audit``, ``fix:<slug>``, ``draft:<slug>``,
  ``task:<n>``, ``seo:next-article``, ``metrics:record-first``), so the
  owner can mark one done from the CLI without re-reading the list.

State shape (kept inside the growth state file, under ``daily_plans``)::

    "daily_plans": {
        "2026-11-07": {
            "items": ["fix:prerender-routes", "approve:reddit-answers"],
            "status": {"fix:prerender-routes": "done"},
        },
    }

Use it from the growth CLI (``python businesses/road2cissp/growth.py
daily-plan``) or from the dashboard (``GET /api/daily-plan``).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_ITEMS = 10

BANNER = ("> **NOTHING WAS PUBLISHED AND NO MARKETING MONEY WAS SPENT.** "
          "This plan only lists moves. Experiments run only after you "
          "approve them at /approvals; every draft waits for you to post it.")

# Priority tiers, lowest first. Within a tier, carried-over items sort
# ahead of new ones, then in the order the sources list them.
_TIER_FIX = 0
_TIER_DECISION = 1
_TIER_REPLY = 2
_TIER_DUE_JOB = 3
_TIER_DRAFT = 4
_TIER_WEEK_TASK = 5
_TIER_SEO = 6
_TIER_METRICS_GAP = 7

_JOB_ORDER = ("audit", "keywords", "metrics", "strategy")

# Default cadence mirrors road2cissp_growth.CADENCE; the growth program
# passes its own so this module never imports it (no import cycle).
DEFAULT_CADENCE = {
    "audit": timedelta(days=7),
    "keywords": timedelta(days=7),
    "metrics": timedelta(days=1),
    "strategy": timedelta(days=7),
}
DEFAULT_RETRY_AFTER = timedelta(hours=6)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60] or "item"


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _cell(value) -> str:
    return str(value if value is not None else "—").replace("\n", " ")


def is_job_due(state: dict, job: str, now: datetime,
               cadence: dict | None = None,
               retry_after: timedelta | None = None) -> bool:
    """Mirror of the growth cycle's due check: cadence elapsed since the
    last successful run, with a back-off after a recent failed attempt."""
    cadence = cadence or DEFAULT_CADENCE
    retry_after = retry_after if retry_after is not None else DEFAULT_RETRY_AFTER
    if job not in cadence:
        return False
    last_run = state.get("last_run", {})
    last_attempt = state.get("last_attempt", {})
    last = _ts(last_run.get(job))
    attempt = _ts(last_attempt.get(job))
    if attempt and (last is None or attempt > last) and now - attempt < retry_after:
        return False
    return last is None or now - last >= cadence[job]


def due_jobs(state: dict, now: datetime, cadence: dict | None = None,
             retry_after: timedelta | None = None) -> list[str]:
    """Jobs due today, in run order: audit, keywords, metrics, strategy."""
    return [job for job in _JOB_ORDER
            if is_job_due(state, job, now, cadence, retry_after)]


def build_daily_plan(*, business_name: str, site_url: str, state: dict,
                     now: datetime | None = None,
                     cadence: dict | None = None,
                     retry_after: timedelta | None = None,
                     max_items: int = MAX_ITEMS) -> dict:
    """Build today's ordered plan from growth state. Pure: no I/O, no LLM,
    no network. ``state`` is the growth state dict (as loaded by
    ``road2cissp_growth.load_state``); it is never mutated here — call
    :func:`record_plan` to persist the built plan."""
    now = now or datetime.now(timezone.utc)
    date = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    plans = state.get("daily_plans") or {}
    today_status = (plans.get(date) or {}).get("status") or {}
    yesterday_items = (plans.get(yesterday) or {}).get("items") or []
    yesterday_status = (plans.get(yesterday) or {}).get("status") or {}

    strategy = state.get("strategy") or {}
    audit = state.get("audit") or {}
    experiments = state.get("experiments") or {}
    metrics = state.get("metrics") or []
    keywords = (state.get("keywords") or {}).get("items", [])

    # Sticky dones: {item_id: source_key} — an item marked done stays
    # hidden while its source is unchanged (see mark_item). Jobs are
    # never sticky: they reappear whenever their cadence says due.
    sticky_done = (plans.get("done") or {})

    # (tier, seq, item) — seq keeps source order inside a tier.
    candidates: list[tuple[int, int, dict]] = []
    seq = 0

    def add(tier: int, item: dict) -> None:
        nonlocal seq
        if item["id"] in today_status:
            return  # done or skipped today — never reappear today
        if (item["kind"] != "run"
                and sticky_done.get(item["id"]) is not None
                and sticky_done.get(item["id"]) == item.get("source_key")):
            return  # done, and its source has not changed since
        carried = (item["id"] in yesterday_items
                   and yesterday_status.get(item["id"]) not in ("done", "skipped"))
        item["carried_over"] = carried
        # Carried-over items sort first inside their tier.
        candidates.append((tier, 0 if carried else 1, seq, item))
        seq += 1

    strategy_key = f"strategy:{strategy.get('cycle') or strategy.get('at') or ''}"
    audit_key = f"audit:{audit.get('at') or ''}"
    seo_key = ("seo:" + ",".join(sorted((state.get("seo_articles") or {}).keys())))

    # Tier 0 — fix what caps everything: P0 site fixes, then critical
    # audit findings. Without crawlability and measurement, the rest
    # of the plan cannot pay off.
    for fix in strategy.get("site_fixes") or []:
        if str(fix.get("priority", "")).upper() != "P0":
            continue
        add(_TIER_FIX, {
            "id": f"fix:{_slug(fix.get('fix', ''))}",
            "channel": "site", "kind": "fix",
            "title": f"Fix (P0): {fix.get('fix', '')}",
            "detail": _cell(fix.get("why")),
            "action": "Fix on the site, then re-run the audit to confirm.",
            "done_when": "The audit no longer reports this finding.",
            "source": "strategy site_fixes (P0)",
            "source_key": strategy_key,
            "needs_llm": False,
        })
    for finding in audit.get("findings") or []:
        if finding.get("severity") != "critical":
            continue
        add(_TIER_FIX, {
            "id": f"fix:{_slug(finding.get('issue', ''))}",
            "channel": "site", "kind": "fix",
            "title": f"Fix (critical): {finding.get('issue', '')}",
            "detail": _cell(finding.get("fix")),
            "action": "Fix on the site, then re-run the audit to confirm.",
            "done_when": "The audit no longer reports this finding.",
            "source": f"site audit ({audit.get('at', '')[:10]})",
            "source_key": audit_key,
            "needs_llm": False,
        })

    # Tier 1 — owner decisions waiting. Nothing else can move until
    # these are answered: an experiment cannot start, and a finished
    # one cannot teach the next strategy, without the owner.
    for slug in sorted(experiments):
        exp = experiments[slug]
        if exp.get("status") in ("proposed", "superseded"):
            add(_TIER_DECISION, {
                "id": f"approve:{slug}",
                "channel": exp.get("channel") or "strategy", "kind": "approve",
                "title": f"Decide: {exp.get('name', slug)}",
                "detail": f"{_cell(exp.get('hypothesis'))} — KPI "
                          f"{_cell(exp.get('kpi'))} → {_cell(exp.get('target'))}.",
                "action": "Approve or reject at /approvals.",
                "done_when": "The experiment is approved or rejected.",
                "source": "proposed experiment",
                "source_key": f"exp:{slug}:{exp.get('status')}",
                "needs_llm": False,
            })

    for slug in sorted(experiments):
        exp = experiments[slug]
        if exp.get("status") == "review":
            add(_TIER_DECISION, {
                "id": f"outcome:{slug}",
                "channel": exp.get("channel") or "strategy", "kind": "record",
                "title": f"Record outcome: {exp.get('name', slug)}",
                "detail": "Its duration ended. What happened — visits, "
                          "sign-ins, what worked, what did not?",
                "action": (f"python businesses/road2cissp/growth.py outcome "
                           f"{slug} --worked \"...\"  (or --failed \"...\")"),
                "done_when": "The experiment is marked completed with an outcome.",
                "source": "finished experiment awaiting review",
                "source_key": f"exp:{slug}:{exp.get('status')}",
                "needs_llm": False,
            })

    # Tier 2 — drafted community replies. A thread goes stale in days,
    # so a reply that is already drafted outranks jobs that can run any
    # day. Only "drafted" threads appear: found-but-undrafted ones are
    # not the owner's move yet, and done/skipped ones are finished.
    replies = [t for t in (state.get("community_threads") or {}).values()
               if t.get("status") == "drafted"]
    replies.sort(key=lambda t: (-(t.get("intent_score") or 0),
                                -(t.get("created_utc") or 0)))
    for thread in replies:
        tid = thread["id"]
        add(_TIER_REPLY, {
            "id": f"reply:{tid}",
            "channel": "community", "kind": "do",
            "title": f"Reply: {str(thread.get('title', ''))[:80]}",
            "detail": f"{thread.get('venue', '')} · intent "
                      f"{thread.get('intent_score', '')}/100 "
                      f"({thread.get('intent', '')}) — "
                      f"{'; '.join(thread.get('reasons', []))}",
            "action": f"Post the draft at {thread.get('url', '')} "
                      f"(draft: {thread.get('draft_path', '')})",
            "done_when": "You posted the reply.",
            "source": "community listening (drafted reply)",
            "source_key": f"thread:{tid}:drafted",
            "needs_llm": False,
        })

    # Tier 3 — jobs due today, in run order. These are the agent jobs;
    # the item is to run the growth cycle, which runs only the due ones.
    for job in due_jobs(state, now, cadence, retry_after):
        if job == "metrics" and not metrics:
            continue  # no measurement exists yet — tier 6 covers that
        add(_TIER_DUE_JOB, {
            "id": f"job:{job}",
            "channel": {"audit": "site", "keywords": "seo",
                        "metrics": "metrics", "strategy": "strategy"}[job],
            "kind": "run",
            "title": f"Run due job: {job}",
            "detail": {"audit": "Live crawl of the site for titles, meta, "
                                "canonical, og:image and crawlable text.",
                       "keywords": "Google Trends keyword map (12 months); "
                                   "gaps are marked 'unavailable', never invented.",
                       "metrics": "Pull the latest traffic snapshot.",
                       "strategy": "Weekly strategy: diagnosis, ranked channel "
                                   "bets, new experiments for approval."}[job],
            "action": "python businesses/road2cissp/growth.py run",
            "done_when": f"The {job} job has run in today's growth cycle.",
            "source": "growth cadence",
            "source_key": None,
            "needs_llm": job in ("strategy",),
        })

    # Tier 4 — drafts for active experiments that still need content.
    for slug in sorted(experiments):
        exp = experiments[slug]
        if exp.get("status") != "active" or exp.get("drafts") is not None:
            continue
        if not exp.get("content_needed"):
            continue
        needed = ", ".join(exp["content_needed"])
        add(_TIER_DRAFT, {
            "id": f"draft:{slug}",
            "channel": exp.get("channel") or "content", "kind": "draft",
            "title": f"Draft content: {exp.get('name', slug)}",
            "detail": f"Needs {needed}. Drafts are files only — you post them.",
            "action": "python businesses/road2cissp/growth.py run",
            "done_when": "Drafts exist under reports/road2cissp_drafts/<experiment>/",
            "source": "active experiment awaiting drafts",
            "source_key": f"exp:{slug}:{exp.get('status')}",
            "needs_llm": True,
        })

    # Tier 5 — this week's strategy tasks, then P1 site fixes.
    for n, task in enumerate(strategy.get("this_week") or [], 1):
        add(_TIER_WEEK_TASK, {
            "id": f"task:{n}",
            "channel": "strategy", "kind": "do",
            "title": _cell(task),
            "detail": "From this week's strategy, highest leverage first.",
            "action": "Do it, then mark it done: daily-plan done task:"
                      f"{n}",
            "done_when": "The task is done.",
            "source": "strategy this_week",
            "source_key": strategy_key,
            "needs_llm": False,
        })
    for fix in strategy.get("site_fixes") or []:
        if str(fix.get("priority", "")).upper() != "P1":
            continue
        add(_TIER_WEEK_TASK, {
            "id": f"fix:{_slug(fix.get('fix', ''))}",
            "channel": "site", "kind": "fix",
            "title": f"Fix (P1): {fix.get('fix', '')}",
            "detail": _cell(fix.get("why")),
            "action": "Fix on the site, then re-run the audit to confirm.",
            "done_when": "The audit no longer reports this finding.",
            "source": "strategy site_fixes (P1)",
            "source_key": strategy_key,
            "needs_llm": False,
        })

    # Tier 6 — one gated SEO article, only once the SEO pipeline is in
    # use for this business (a keyword map or drafted articles exist).
    if keywords or state.get("seo_articles"):
        add(_TIER_SEO, {
            "id": "seo:next-article",
            "channel": "seo", "kind": "draft",
            "title": "Draft the next gated SEO article",
            "detail": "One article from the live keyword map. It must pass "
                      "the deterministic quality gate; the draft is never "
                      "published automatically.",
            "action": "python businesses/road2cissp/growth.py seo --count 1",
            "done_when": "A new draft exists under reports/seo/road2cissp_com/.",
            "source": "SEO engine (gated article pipeline)",
            "source_key": seo_key,
            "needs_llm": True,
        })

    # Tier 7 — no measurement at all. Fix measurement before scaling
    # anything: without it, no experiment can be judged.
    if not metrics:
        add(_TIER_METRICS_GAP, {
            "id": "metrics:record-first",
            "channel": "metrics", "kind": "record",
            "title": "Get one measurement on the books",
            "detail": "No traffic numbers yet. Install analytics (see the "
                      "audit) or record yesterday's numbers by hand.",
            "action": ("python businesses/road2cissp/growth.py record-metrics "
                       "--visitors 0 --signups 0 --note \"first numbers\""),
            "done_when": "One metrics entry exists in the growth state.",
            "source": "measurement gap",
            "source_key": "metrics:empty",
            "needs_llm": False,
        })

    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    items = [c[3] for c in candidates]
    truncated = max(0, len(items) - max_items)
    items = items[:max_items]
    for rank, item in enumerate(items, 1):
        item["rank"] = rank

    north_star = (strategy.get("north_star") or {})
    return {
        "business": business_name,
        "site_url": site_url,
        "date": date,
        "generated_at": now.isoformat(),
        "north_star": {
            "metric": north_star.get("metric"),
            "current": north_star.get("current"),
            "target_90d": north_star.get("target_90d"),
        },
        "items": items,
        "truncated": truncated,
        "counts": {
            "total": len(items),
            "carried_over": sum(1 for i in items if i["carried_over"]),
            "needs_owner": sum(1 for i in items
                               if i["kind"] in ("approve", "record", "do", "fix")),
        },
    }


def record_plan(state: dict, plan: dict) -> None:
    """Persist a built plan's item ids in the growth state so tomorrow
    can carry over unfinished items. Existing done/skipped marks for
    that date are kept, as are sticky dones (see :func:`mark_item`)."""
    plans = state.setdefault("daily_plans", {})
    plans.setdefault("done", {})
    entry = plans.setdefault(plan["date"], {"items": [], "status": {}})
    entry["items"] = [item["id"] for item in plan["items"]]
    entry.setdefault("status", {})


def mark_item(state: dict, date: str, item_id: str, status: str,
              source_key: str | None = None) -> None:
    """Mark one item on a recorded plan ``done`` or ``skipped``.
    Raises ValueError for a bad status or an item not on that day's plan.

    ``done`` is sticky for non-job items: the item stays hidden on
    later days while its ``source_key`` is unchanged (the source has
    not produced a new version of the work). Pass the item's
    ``source_key`` from the built plan so the stickiness knows what
    "unchanged" means; without it, done only hides the item for that
    day. ``skipped`` always defers to tomorrow only. Jobs (``job:*``)
    are never sticky — their cadence decides when they return."""
    if status not in ("done", "skipped"):
        raise ValueError(f"status must be 'done' or 'skipped', got {status!r}")
    plans = state.setdefault("daily_plans", {})
    entry = plans.get(date)
    if entry is None or item_id not in (entry.get("items") or []):
        raise ValueError(f"item {item_id!r} is not on the {date} daily plan "
                         f"(build it first with `daily-plan`)")
    entry.setdefault("status", {})[item_id] = status
    if status == "done" and source_key is not None \
            and not item_id.startswith("job:"):
        plans.setdefault("done", {})[item_id] = source_key


def report_name(date: str) -> str:
    return f"daily_plan_{date.replace('-', '')}.md"


def render_markdown(plan: dict) -> str:
    """The plan as a Markdown report, in the same voice as the weekly
    growth report: your move first, then the list, banner included."""
    out = [f"# Daily plan — {plan['business']} ({plan['date']})", "",
           f"_Generated {plan['generated_at']} · {plan['site_url']}_", "",
           BANNER, ""]
    ns = plan.get("north_star") or {}
    if ns.get("metric"):
        out.append(f"**North star:** {_cell(ns.get('metric'))} — now "
                   f"{_cell(ns.get('current'))}, 90-day target "
                   f"{_cell(ns.get('target_90d'))}.")
        out.append("")
    if not plan["items"]:
        out.append("Nothing needs you right now.")
    for item in plan["items"]:
        carried = " _(carried over)_" if item["carried_over"] else ""
        llm = " _(needs LLM key)_" if item.get("needs_llm") else ""
        out.append(f"## {item['rank']}. {item['title']}{carried}{llm}")
        out.append("")
        out.append(f"- **Channel:** {item['channel']} · **Kind:** {item['kind']}")
        out.append(f"- **Why:** {item['detail']}")
        out.append(f"- **Do:** `{item['action']}`")
        out.append(f"- **Done when:** {item['done_when']}")
        out.append("")
    if plan["truncated"]:
        out.append(f"_…and {plan['truncated']} more waiting in the weekly "
                   f"report. The daily list caps at {MAX_ITEMS}._")
        out.append("")
    out.append("---")
    out.append(BANNER)
    out.append("")
    return "\n".join(out)


def render_text(plan: dict) -> str:
    """The plan as plain text for the CLI: one numbered line per move."""
    lines = [f"Daily plan — {plan['business']} ({plan['date']})"]
    ns = plan.get("north_star") or {}
    if ns.get("metric"):
        lines.append(f"North star: {ns['metric']} — now {ns.get('current')}, "
                     f"90-day target {ns.get('target_90d')}")
    if not plan["items"]:
        lines.append("Nothing needs you right now.")
    for item in plan["items"]:
        carried = " [carried over]" if item["carried_over"] else ""
        llm = " [needs LLM key]" if item.get("needs_llm") else ""
        lines.append(f"{item['rank']}. [{item['channel']}] {item['title']}"
                     f"{carried}{llm}")
        lines.append(f"   {item['detail']}")
        lines.append(f"   Do: {item['action']}")
    if plan["truncated"]:
        lines.append(f"…and {plan['truncated']} more in the weekly report.")
    return "\n".join(lines) + "\n"
