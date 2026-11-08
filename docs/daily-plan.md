# Daily plan

Every morning, one ordered list of today's marketing moves for
road2cissp.com — the weekly growth cycle, turned into a daily list
the way HeyCatch sells it, but built on the ecosystem's own engine.

```bash
python businesses/road2cissp/growth.py daily-plan            # today (UTC)
python businesses/road2cissp/growth.py daily-plan --date 2026-11-08
python businesses/road2cissp/growth.py daily-plan-done fix:prerender-routes
python businesses/road2cissp/growth.py daily-plan-skip task:2
```

Or read it from the dashboard: `GET /api/daily-plan` (also linked
from the portfolio page).

## Order

1. **P0 site fixes + critical audit findings** — crawlability and
   measurement cap everything else.
2. **Owner decisions waiting** — approve/reject proposed experiments,
   record outcomes for finished ones.
3. **Drafted community replies** — a thread goes stale in days, so a
   reply that is already drafted outranks jobs that can run any day.
   Marking one done means you posted it by hand.
4. **Jobs due today** — audit, keywords, metrics, strategy, in run
   order, only when their cadence says they are due (with the same
   6-hour back-off after a failed attempt as the weekly cycle).
5. **Drafts for active experiments** that still need content.
6. **This week's strategy tasks**, then P1 site fixes.
7. **Next gated SEO article** — one, quality-gated, draft only.
8. **Measurement gap** — if no traffic numbers exist at all.

At most 10 items. Anything beyond the cap stays in the weekly report;
the count is shown as "…and N more".

## Rules

- **Building a plan runs nothing.** No agent is dispatched, no money
  is spent, nothing is published. Every item names the exact command
  (or dashboard page) that does the work; you run it.
- **Carry-over, not amnesia.** An item on yesterday's plan that was
  not marked done or skipped comes back today, flagged "carried over",
  ahead of same-tier new items.
- **Done is sticky, skip is for today.** Marking an item done hides
  it until its source changes — a new strategy cycle, a new audit, a
  new experiment status, a newly drafted article. The work is
  finished, so it must not nag you again just because the old
  strategy text is still in the state file. Skipping only defers it
  to tomorrow. Due jobs (`job:*`) are the exception: they are never
  sticky-done. A job reappears whenever its cadence says it is due
  again, because marking a job done without running it would be a
  lie — run it, and its `last_run` moves.
- **Item ids are stable for the day** (`approve:<slug>`,
  `outcome:<slug>`, `reply:<thread-id>`, `job:audit`, `fix:<slug>`,
  `draft:<slug>`, `task:<n>`, `seo:next-article`,
  `metrics:record-first`), so you can mark one done from the CLI
  without re-reading the list. Marking a `reply:<thread-id>` done
  also flips that thread to "done" in the listening state, so the
  plan never lists it again.
- **Deterministic.** The builder (`ecosystem/daily_plan.py`) reads
  only the growth state dict and the clock: no LLM, no network. The
  same state + date always builds the same plan.

## Where it lives

- Builder: `ecosystem/daily_plan.py` (`build_daily_plan`,
  `record_plan`, `mark_item`, `render_markdown`, `render_text`).
- CLI: `daily-plan`, `daily-plan-done`, `daily-plan-skip` in
  `businesses/road2cissp/growth.py`. Building a plan records its item
  ids in `data/road2cissp_growth.json` under `daily_plans`, and writes
  `reports/daily_plan_<yyyymmdd>.md`.
- Dashboard: `GET /api/daily-plan` in `ventoryn/app.py`. It builds
  (but does not record) the plan from the growth state file
  (`DAILY_PLAN_STATE_PATH` overrides the path, for tests).
- Tests: `tests/test_daily_plan.py` — ranking, due/back-off,
  carry-over, done/skip, the 10-item cap, determinism, CLI, dashboard.

## What it is not (yet)

- Not a second growth program: it reuses the weekly cycle's state,
  cadence, experiments and strategy. It adds ordering, not new jobs.
- Not Farelune (yet): the builder is generic — business name, site
  URL, state and cadence are arguments — but only the Road to CISSP
  CLI and dashboard endpoint wire it up so far. Farelune's program
  is mission-based with no equivalent state file; wiring it in is a
  follow-up, not part of this change.
- Not community listening, video, or funnel attribution: those are
  the next gaps on the HeyCatch map, in that order.
