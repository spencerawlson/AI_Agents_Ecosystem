# Ventoryn — Live Ops

The dashboard's professional Overview:
**http://localhost:8000/ops**. `/game` redirects to it (303) — the
old game address is gone, and the game file with it. The page is
server-rendered through the platform shell (purple sidebar + white
workspace), all from real store data. No game design: no crew, no
HUD, no runtime controls, no Experiment 001 on this page.

## What it shows

- **The top summary** — the progress ring (verified / 100 paying
  customers) and the Verified / Pending / Unattributed stat cards,
  plus the attribution flow, the conversion donut, the campaign
  table, the privacy toggles, the SVG acquisition trend (real data
  only), the breakdown bars, and the data-source legend. Same
  sections as the customer report — they are rendered from one
  shared helper, so the two can never disagree.
- **The pause control (operator only)** — "Pause all agents" /
  "Resume all agents". The only way to stop the agents working:
  pausing stops the orchestrator tick loop (no ticks run while
  paused, including automatic ticking) and survives a dashboard
  restart, because the flag lives in the shared state DB. The same
  control sits on My account (Settings) for the operator.
  On-demand analyze jobs are untouched by the pause.
- **Analyze a site** — the URL box (below), also on Portfolio.
- **Businesses** — the websites list, with revenue / costs / net
  profit per site, "hidden" when the site hides its revenue.
- **Live activity** — the raw audit events, newest first — time,
  agent, event, business, task, and the result. This is what the
  agents are actually doing in the background, not a paraphrase.
  It is the only feed on Overview: "What just happened" and "What
  the team is doing" (tasks & spend per agent bars + activity over
  time) live on the Agents page (`/agents`).

## Where the data comes from

Everything is `GET /api/snapshot` plus the attribution data
(`_attribution_data()`): tick, per-agent stats (`tasks_completed`,
`tokens_used`, `cost_usd`, `last_task`), business P&L rows, totals,
and the latest 50 audit events. No new backend was added for this
view. The snapshot shape is unchanged — DJINN reads it.

## What persists

The audit log lives in the same SQLite file as the businesses and
the ledger (`ECOSYSTEM_STATE_DB`, `core/state_store.py`
`SharedAuditLog`):

- Every tick records **one event per agent run** (`run_completed`,
  with tokens + cost and a result summary — opportunities found,
  reports written) plus the `tick_completed` summary, so the feed
  can say what each agent did and what it spent. Failed runs are
  recorded as `run_failed`, with the error.
- *Tasks & spend per agent* (on the Agents page) is computed from
  that same log, not from a second tally: one run counts as one task
  and its cost/tokens are counted once, even when the run produced
  both a `run_completed` and a `task_verified` event for it. A
  failed or reworked run counts its spend but not a completed task.
  Summary events (`tick_completed`) never move the bars. So the
  bars, the feed and the header costs now agree — they all read the
  same record.
- The feed shows the cost next to an event when the run spent
  anything (e.g. "$0.02 · 1,240 tokens"); older cost-less events
  and $0 heuristic runs show nothing.
- The table keeps the **latest 500 events**; older ones are pruned.
- Without `ECOSYSTEM_STATE_DB` the audit stays in-memory (tests,
  one-off runs) and a restart still clears it.
- Reset (`/api/reset`) rebuilds the runtime but does not wipe the
  audit log: the history is a record, not session state.

## Analyze a site

The **Analyze a site** panel runs the same analysis as the
`analyze` CLI from the page: website URL + market country (default
`ca`) + optional language. Submitting starts a background job —
`POST /api/analyze` returns a job id (202) and the panel polls
`GET /api/analyze/{job_id}` every 3s until it is done or failed,
because a live crawl takes tens of seconds. The result shows the
site score, the GEO score and the top findings, plus a link to the
saved report (`GET /api/analyze/{job_id}/report`). The report is
written to `reports/analyze_<host>_<date>.md` exactly like the CLI
writes it. Nothing is published or posted anywhere; the feature
spends nothing.

The URL is validated server-side (http/https only; empty, non-URL,
localhost and private addresses are rejected with a 400). A site
that cannot be fetched at all gets **no scores** on the page — it
shows "Unavailable — could not fetch" with the reason instead of a
number built from fetch failures. (The saved report file still
carries the CLI's own computed numbers next to the critical
"could not be fetched" finding, exactly as the CLI writes it.)

A visitor who typed a URL on the landing page arrives with
`?site_url=` — the box prefills it, and a customer who has not
subscribed yet sees the "Subscribe — $20/month to add it" prompt
for that URL, so it is never retyped.

## Honest limits

- When nothing has happened yet, the panels say so ("Nothing yet
  — agent work will show up here", "No work recorded yet") instead
  of inventing activity. An empty feed is a quiet crew, not a
  broken page. "No work recorded yet" means the log holds no run
  events at all — a tick that only failed still shows its spend in
  the bars.
- Mission scripts (the road2cissp / Farelune growth programs) don't
  go through the shared runtime, so their spend still doesn't appear
  in the bars or the feed — only dashboard and worker ticks do.
- The "Last did" line shows the agent's last recorded task, which
  may be from an earlier tick — it is a record, not a live camera.
- The Agents page's activity-over-time chart is built from the
  persisted audit events (events per day, last 7 days), not from a
  browser-memory poll history.

## Run it

```bash
python launch.py            # dashboard → http://localhost:8000
```

Then open `/ops`. One tick = one real orchestrator tick; the
worker ticks on its interval unless the operator paused all agents.
