# Collegium — Live Ops

The dashboard's live view of the crew at work:
**http://localhost:8000/ops** (`/game` still works — the same page
under its old address).

## What it shows

- **The crew** — 8 vector characters (inline SVG, no emoji, no
  raster images): Scout (telescope), Analyst (microscope), Builder
  (hammer + wrench), Studio (palette), Growth (megaphone), Care
  (chat), Ops (gear), Ledger (ledger book). Each card shows its
  tasks, tokens and cost, and — for real agents — **"Last did: …"**,
  the last task from the API. Builder shows the business count;
  Ledger shows the ledger totals.
- **What the team is doing** — the live ops window:
  - *Live activity feed*: the raw audit events, newest first —
    time, agent, event, business, task, and the result. This is
    what the agents are actually doing in the background, not a
    paraphrase.
  - *Tasks & spend per agent*: a hand-built inline SVG bar chart
    (blue = tasks, orange = spend), one row per agent. The bars are
    computed from the audit log — the same source as the feed — so
    they move whenever the feed does, for every agent, and survive
    a restart with the shared state on.
  - *Activity over time*: a hand-built inline SVG line chart of
    total tasks completed, one point per poll.
- **Businesses, What Just Happened, Experiment 001, HUD totals** —
  as before. Step / Auto / Pause / Reset work the same way.

## Where the data comes from

Everything is `GET /api/snapshot` — tick, per-agent stats
(`tasks_completed`, `tokens_used`, `cost_usd`, `last_task`),
business P&L rows, totals, and the latest 50 audit events. No new
backend was added for this view. The page polls `/api/snapshot`
every 5 seconds (skipped while the tab is hidden), so the feed and
graphs move while ticks run elsewhere — Auto, the worker, another
tab.

## What persists

The feed used to be amnesiac: the ledger survived restarts (shared
SQLite) but the audit log lived only in memory, so a dashboard
restart emptied "What the team is doing" while the money stayed —
and a worker process's events never reached the dashboard at all,
since they died with the worker. Now the audit log lives in the
same SQLite file as the businesses and the ledger
(`ECOSYSTEM_STATE_DB`, `core/state_store.py` `SharedAuditLog`):

- Every tick records **one event per agent run** (`run_completed`,
  with tokens + cost and a result summary — opportunities found,
  reports written) plus the `tick_completed` summary, so the feed
  can say what each agent did and what it spent. Failed runs are
  recorded as `run_failed`, with the error.
- *Tasks & spend per agent* is computed from that same log, not from
  a second tally: one run counts as one task and its cost/tokens are
  counted once, even when the run produced both a `run_completed`
  and a `task_verified` event for it. A failed or reworked run
  counts its spend but not a completed task. Summary events
  (`tick_completed`) never move the bars. So the bars, the feed and
  the header costs now agree — they all read the same record.
- The feed shows the cost next to an event when the run spent
  anything (e.g. "$0.02 · 1,240 tokens"); older cost-less events
  and $0 heuristic runs show nothing.
- The table keeps the **latest 500 events**; older ones are pruned.
- Without `ECOSYSTEM_STATE_DB` the audit stays in-memory (tests,
  one-off runs) and a restart still clears it.
- Reset (`/api/reset`) rebuilds the runtime but does not wipe the
  audit log: the history is a record, not session state.

## Analyze a site

Below the ops window, the **Analyze a site** panel runs the same
analysis as the `analyze` CLI from the page: website URL + market
country (default `ca`) + optional language. Submitting starts a
background job — `POST /api/analyze` returns a job id (202) and the
page polls `GET /api/analyze/{job_id}` on the same 5s rhythm until
it is done or failed, because a live crawl takes tens of seconds.
The result shows the site score, the GEO score and the top findings,
plus a link to the saved report
(`GET /api/analyze/{job_id}/report`). The report is written to
`reports/analyze_<host>_<date>.md` exactly like the CLI writes it.
Nothing is published or posted anywhere; the feature spends nothing.

The URL is validated server-side (http/https only; empty, non-URL,
localhost and private addresses are rejected with a 400). A site
that cannot be fetched at all gets **no scores** on the page — it
shows "Unavailable — could not fetch" with the reason instead of a
number built from fetch failures. (The saved report file still
carries the CLI's own computed numbers next to the critical
"could not be fetched" finding, exactly as the CLI writes it.)

## Phones: crew carousel

On phones (≤560px) the crew section is a one-card-at-a-time
carousel instead of a stacked list: a horizontally scrollable strip
with `scroll-snap-type: x mandatory`, one card per view, scrollbar
hidden, native touch swiping (works on iOS Safari), prev/next arrow
buttons (44px+ tap targets) and dots plus "n of 8" text that track
the position on scroll. The 5s re-render of the cards saves and
restores the scroll position, so swiping to an agent doesn't snap
back. Desktop keeps the 4-column grid, untouched. Each card still
shows what that agent is doing (tasks, spend, last task) — the
carousel only changes how you move between cards on a phone.

## Honest limits

- The activity line is a **rolling in-page history** (last 30
  polls). It resets on reload; the status line says so. The bars,
  feed and cards always come from the server, so they survive a
  reload — and a restart too, when shared state is on.
- When nothing has happened yet, the panels say so ("Nothing yet
  — run a tick", "No work recorded yet") instead of inventing
  activity. An empty feed is a quiet crew, not a broken page.
  "No work recorded yet" means the log holds no run events at all —
  a tick that only failed still shows its spend in the bars.
- Mission scripts (the road2cissp / Farelune growth programs) don't
  go through the shared runtime, so their spend still doesn't appear
  in the bars or the feed — only dashboard and worker ticks do.
- The "Last did" line shows the agent's last recorded task, which
  may be from an earlier tick — it is a record, not a live camera.

## Run it

```bash
python launch.py            # dashboard → http://localhost:8000
```

Then open `/ops`. One tick = one real orchestrator tick; Auto runs
ticks on the speed interval (1×/2×/4×).
