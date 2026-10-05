# AI Autonomous Micro-Business Ecosystem

A multi-agent software platform that researches, creates, launches, advertises, operates, monitors, and optimizes a portfolio of small online businesses — an AI-powered business factory, not a single automated store.

## Objective

Build a diversified portfolio generating **$60,000+ USD/month in sustainable net profit**, working backward from that target and reporting progress against it.

The system runs a continuous lifecycle across many business models (e-commerce, print-on-demand, digital products, micro-SaaS, affiliate/content, B2B services, and others discovered by research):

```
Discover → Research → Validate → Build → Launch → Market →
Operate → Measure → Optimize → Scale or Shut Down → Reinvest
```

## First milestone

Not $60K. The first engineering/business milestone is:

> Use the platform to identify, create, launch, operate, and optimize **one legitimate business** that generates its **first $1,000 in real customer revenue** with **positive unit economics**.

Then prove it is repeatable.

```
$0 → First customer → $100 → $1,000 → Positive unit economics →
Second business → $1K/mo profit → $5K → $10K → $25K → $60K+
```

## Watch it work — Simulation Game

A game-like live simulation: launch it and watch the agents work as characters — discovering opportunities, researching, launching businesses, running marketing campaigns, handling support tickets, and reacting to incidents in real time.

Open the **Simulation Game** artifact to play it.

## Architecture

Hierarchical multi-agent system. A central orchestrator coordinates specialized agents. Humans control consequential decisions and capital.

```
                     HUMAN OWNER
                          │
                          ▼
                 CONTROL / APPROVAL
                          │
                          ▼
                 ORCHESTRATOR (DJINN)
                          │
       ┌──────────────────┼──────────────────┐
       │                  │                  │
       ▼                  ▼                  ▼
   DISCOVERY          OPERATIONS          FINANCE
    AGENTS              AGENTS             AGENTS
       │                  │                  │
       └──────────────────┼──────────────────┘
                          │
                          ▼
                 BUSINESS PORTFOLIO
```

The orchestrator is an internal privileged decision layer. Stores, customers, and public-facing apps never get direct access to it.

**Core principle:** AI automates execution. Data determines recommendations. Humans control consequential decisions and capital.

## Repository layout

```
├── orchestrator/     Central decision and coordination engine
│                     (models, registry, engine, approvals)
├── agents/           Specialized agents:
│                       discovery, research, marketing, creative,
│                       support, operations (+ base framework)
├── core/             Shared systems:
│                       registry, ledger, experiments, scoring,
│                       audit, analytics, reporting, allocation,
│                       portfolio, memory, predictive, optimization,
│                       intelligence, integrations, shutdown
├── dashboard/        Portfolio dashboard (FastAPI + HTML)
├── infra/            Docker Compose (PostgreSQL 16, Redis 7),
│                     SQLAlchemy models, Postgres task store
├── docs/             Architecture, roadmap
├── examples/         End-to-end pipeline demos
└── tests/            36 tests, all passing
```

## Development phases

| Phase | Focus | Status | What's built |
|-------|-------|--------|--------------|
| 1 | Business Factory MVP | ✅ Scaffolded + hardened | Orchestrator, agent framework, registry, experiments, ledger, approvals, dashboard, audit, PostgreSQL layer, XSS fix, structured logging, budget enforcement, `pyproject.toml` |
| 2 | Operations | ✅ Built | Marketing (budget-capped campaigns), support (triage + escalation), creative (versioned assets), operations (incidents), analytics (funnels, cohorts), reporting (P&L, cash flow), integrations (commerce adapter) |
| 3 | Portfolio Management | ✅ Built | Capital allocator (efficiency-ranked, beats equal-split), cross-business benchmarks, portfolio experiments (global cap), shutdown workflows |
| 4 | Advanced Intelligence | ✅ Built | Agent memory, predictive opportunity ranking, dynamic pricing, ROAS reallocation, anomaly detection, model routing/cost optimization |
| 5 | Scale | ⬜ Not started | Infrastructure scale-out only when demand requires it |

See [docs/roadmap.md](docs/roadmap.md) for the detailed build plan.

## Business lifecycle

Every business follows a measurable state machine:

```
DISCOVERED → RESEARCHING → VALIDATED → APPROVED → BUILDING →
TESTING → LAUNCHED → OPERATING → OPTIMIZING → SCALING / PAUSED →
MATURE (or TERMINATED)
```

Failed experiments are documented and terminated; their data stays available so future agents learn from them.

## Key invariants

- **Revenue alone never determines success.** Contribution profit and unit economics decide.
- **New businesses start as experiments** with fixed budgets, durations, and stop-loss conditions.
- **Consequential actions require human approval** (spending increases, contracts, fund movements, business deletion).
- **AI inference is a tracked cost** — an agent spending $1K/mo on a $500/mo business gets flagged.
- **Every agent action is auditable** — what it did, why, with what data, at what cost, to what effect.

## Quick start

```bash
pip install -e .
python -m pytest tests/ -q                # 39/39 passing
python launch.py                          # dashboard → http://0.0.0.0:8000
python launch.py worker                   # headless agent tick loop
python launch.py all                      # dashboard + worker together
```

Open **http://0.0.0.0:8000/game** for the live game view: agent stations,
event feed, portfolio HUD, speed controls (1×/2×/4×), pause, and reset —
every tick runs the real orchestrator, agents, ledger, and state machine
via the dashboard's JSON API (`/api/snapshot`, `/api/tick`, `/api/reset`).
No simulation, no mocks.

`launch.py` commands: `dashboard` (default), `worker`, `all`, `initdb`.
Worker flags go after `--`: `python launch.py worker -- --ticks 5 --interval 30`.
Add `--use-postgres` (with `DATABASE_URL` set) to persist tasks in Postgres
instead of the in-memory store.

### Run on a Fedora VM (Proxmox)

```bash
sudo dnf install -y python3.12 python3-pip git
git clone https://github.com/spencerawlson/AI_Agents_Ecosystem.git
cd AI_Agents_Ecosystem
python3 -m venv .venv && source .venv/bin/activate
pip install -e . "uvicorn[standard]" pytest
python launch.py all                      # dashboard + worker
```

Then open `http://<vm-ip>:8000` from your laptop — no desktop needed on the VM.

To survive reboots, install the systemd user service:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/ecosystem.service ~/.config/systemd/user/
# edit paths in the unit if your checkout lives elsewhere
systemctl --user daemon-reload
systemctl --user enable --now ecosystem.service
sudo loginctl enable-linger $USER        # keep running after logout
journalctl --user -u ecosystem.service -f # logs
```

### Postgres + Redis (optional)

The in-memory store is the default. For durable task history:

```bash
docker compose -f infra/docker-compose.yml up -d
export DATABASE_URL=postgresql://ecosystem:ecosystem@localhost:5432/ecosystem
python launch.py initdb
python launch.py all --use-postgres
```

### Experiment 001 monitor (Etsy)

The worker automatically checks the live Etsy shop against the experiment
charter ($1,000 / 60 days, 84+ orders, stop-loss triggers) every 10 ticks:

```bash
python launch.py worker -- --monitor-every 5   # check every 5 ticks
python launch.py worker -- --monitor-once      # single check, then exit
python launch.py worker -- --monitor-every 0   # disable
```

Status is also live at `http://<vm-ip>:8000/experiment-001` on the dashboard.
OAuth tokens auto-refresh; credentials resolve from `ETSY_*` env vars or
`~/.config/evergreen-etsy/`. Ad/fee spend isn't visible via the Etsy API —
record it with:

```bash
python -c "from ecosystem.etsy_monitor import EtsyMonitor;
print(EtsyMonitor.record_spend(10.0, 'Etsy Ads top-up'))"
```

### Shopify store (dropshipping)

Trend-checked product discovery, supplier sourcing priced at a 60% margin,
owner-approved publishing to Shopify, Meta/Google ads built paused and
launched only on approval, plus an automatic spend guard, profit tracking,
SEO fixes and blog drafts. Setup and approval rules:
[docs/shopify-store.md](docs/shopify-store.md).

```bash
python ecosystem/shopify_pipeline.py check
python ecosystem/shopify_pipeline.py discover --niche "home office" --llm --source
```

### Real LLM inference

Agents run on heuristics by default. With an API key, discovery and
research use real LLM inference (cheap tier), and capital allocation
gets smart-tier rationales — all fallback-safe (any LLM failure
degrades to heuristics, never crashes a tick):

```bash
# 1. Key: OpenAI dashboard (https://platform.openai.com/api-keys) → Create key
# 2. On the VM:
export OPENAI_API_KEY="sk-..."         # also in ~/.bashrc or the systemd unit
pip install -r requirements.txt      # pulls litellm
python launch.py worker -- --llm     # force LLM mode
python launch.py worker -- --no-llm  # force heuristics (default when no key)
```

Env overrides: `ECOSYSTEM_CHEAP_MODEL` (default `gpt-6-luna`),
`ECOSYSTEM_SMART_MODEL` (default `gpt-6.1-sol`).
`GEMINI_API_KEY` / `ANTHROPIC_API_KEY` also work — the model strings are
litellm names, so any provider can be swapped in.

Routing: tier 1–2 tasks (discovery, research) → cheap model; tier 3
(capital allocation rationales) → smart model. Real token usage and
cost (via `litellm.completion_cost`) flow through the same budget
enforcement and Ledger accounting as before — expect **cents per
hundred ticks** (real inference is ~50–100x cheaper than the old
heuristic cost rates).

### Real market data

When LLM mode is on, prompts are grounded in live market numbers —
Etsy listing counts + price bands and 12-month Google Trends direction —
so `why_now` and competitor evidence cite real data instead of guesses:

```bash
python launch.py worker -- --no-market  # disable (default: ON with LLM)
```

- **Sources:** Etsy `listings/active` search (reuses the shop's existing
  OAuth token — no new key; token auto-refreshes on 401) and Google
  Trends via `pytrends` (keyless).
- **Discovery** sees a `LIVE MARKET DATA` block for watchlist niches and
  is instructed to prefer rising trends + healthy price bands.
- **Research** gets the niche's real competitor count and price band and
  must use them for `competitor_count` / `price_range_usd`.
- **Rate limits:** snapshots are TTL-cached per keyword set (1h default),
  so repeated ticks don't hammer the APIs. If one source fails, the
  prompt still goes out with the other's data (`partial`); only if both
  fail does the prompt go data-free. A data failure never crashes a tick.

## Status

Phases 1–4 scaffolded and tested (36/36 passing). Next: pick the first real experiment — one low-capital business, defined budget, KPIs, duration, stop-loss — and run it toward the $1,000 revenue / positive-unit-economics milestone. See [docs/roadmap.md](docs/roadmap.md).
