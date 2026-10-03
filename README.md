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
python examples/opportunity_pipeline.py   # discovery → research → scoring → registry
python -m pytest tests/ -q                # or run each tests/test_*.py directly
```

## Status

Phases 1–4 scaffolded and tested (36/36 passing). Next: pick the first real experiment — one low-capital business, defined budget, KPIs, duration, stop-loss — and run it toward the $1,000 revenue / positive-unit-economics milestone. See [docs/roadmap.md](docs/roadmap.md).