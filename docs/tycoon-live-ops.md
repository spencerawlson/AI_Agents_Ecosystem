# Ecosystem Tycoon — Live Ops

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
    (blue = tasks, orange = spend), one row per agent.
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

## Honest limits

- The activity line is a **rolling in-page history** (last 30
  polls). It resets on reload; the status line says so. The bars,
  feed and cards always come from the server, so they survive a
  reload.
- When nothing has happened yet, the panels say so ("Nothing yet
  — run a tick", "No work recorded yet") instead of inventing
  activity. An empty feed is a quiet crew, not a broken page.
- The "Last did" line shows the agent's last recorded task, which
  may be from an earlier tick — it is a record, not a live camera.

## Run it

```bash
python launch.py            # dashboard → http://localhost:8000
```

Then open `/ops`. One tick = one real orchestrator tick; Auto runs
ticks on the speed interval (1×/2×/4×).
