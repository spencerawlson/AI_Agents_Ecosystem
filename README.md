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
├── agents/           Specialized agents (discovery, research, planning,
│                     creation, creative, marketing, support, operations, finance)
├── core/             Shared systems: registry, ledger, experiments,
│                     scoring, capital allocation, approvals, audit
├── dashboard/        Central portfolio dashboard
├── infra/            Docker, PostgreSQL, Redis, deployment
├── docs/             Architecture, data model, roadmap, decisions
└── tests/
```

## Development phases

| Phase | Focus | Goal |
|-------|-------|------|
| 1 | Business Factory MVP | Orchestrator, agent framework, registry, experiments, ledger, approvals, dashboard, audit — launch **one real business** |
| 2 | Operations | Marketing, support, analytics, integrations |
| 3 | Portfolio Management | Multiple businesses, capital allocation, shutdown workflows |
| 4 | Advanced Intelligence | Agent memory, predictive models, opportunity ranking |
| 5 | Scale | Infrastructure scale-out only when demand requires it |

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

## Status

Phase 1 — groundwork in progress. See [docs/roadmap.md](docs/roadmap.md).
