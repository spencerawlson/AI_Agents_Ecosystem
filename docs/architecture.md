# Architecture

## Agent hierarchy

```
                     HUMAN OWNER
                          │
                    Approval gates
                          │
                          ▼
                 ORCHESTRATOR (DJINN)
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
         DISCOVERY     OPERATIONS     FINANCE
```

- **Orchestrator (DJINN):** internal privileged decision layer. Schedules agent tasks, routes recommendations through approval gates, owns the capital allocation engine and the experiment lifecycle. Never exposed to stores, customers, or public apps.
- **Discovery agents:** find opportunities (niche, trend, keyword, product, competitor, marketplace research). Output structured opportunities, not prose.
- **Operations agents:** build, market, support, and monitor businesses (creation, creative, marketing, customer service, operations).
- **Finance agents:** ledger, unit economics, capital allocation recommendations.

## Agent contract

Every agent:

1. Receives a **task** with typed inputs, a budget (dollars and tokens), and a deadline.
2. Produces **structured output** (no free-form prose as a primary artifact).
3. Emits **auditable events** for every consequential action: agent, task, business, inputs, data sources, decision, action, timestamp, cost, result, approval status.
4. Operates within **permission boundaries** — least privilege, credential isolation per business.

## Data model (core entities)

```
businesses, products, services, customers, orders, transactions,
expenses, campaigns, advertisements, suppliers, experiments,
opportunities, agents, agent_tasks, agent_runs, approvals,
recommendations, integrations, documents, events, audit_logs, metrics
```

See [data-model.md](data-model.md) for field-level detail.

## Experiment lifecycle

New businesses start as experiments with fixed parameters:

```yaml
experiment: BUS-0042
type: digital_product
initial_capital: 500        # USD
max_advertising: 300        # USD
duration_days: 30
targets:
  cac_max: 20
  conversion_min: 0.025
  gross_margin_min: 0.50
  roas_min: 2.5
  refund_rate_max: 0.05
```

At the end, the system evaluates actuals and recommends SCALE / MAINTAIN / OPTIMIZE / PAUSE / SHUT DOWN. Budget increases require human approval.

## Business state machine

```
DISCOVERED → RESEARCHING → VALIDATED → APPROVED → BUILDING →
TESTING → LAUNCHED → OPERATING → OPTIMIZING → SCALING | PAUSED → MATURE
                                                          ↘ TERMINATED
```

Transitions are driven by measurable conditions plus approval policy. Terminated businesses keep their data for learning.

## Approval gates

| Action | Threshold | Requirement |
|--------|-----------|-------------|
| Ad spend | $0–50/day | Agent permitted within approved budget |
| Ad spend | $51–250/day | Human approval |
| Ad spend | >$250/day | Human approval + secondary confirmation |
| Fund movements, contracts, account changes, business deletion | any | Human approval |

Thresholds are configurable, not hard-coded.

## Finance

Contribution profit per business:

```
Revenue
− COGS, shipping, marketplace fees, payment processing
− advertising, refunds, chargebacks
− hosting, API costs, AI inference, subscriptions
= Net operating profit
```

Tracked per business: revenue, margins, CAC, LTV, ROAS, conversion, refund rate, AOV, churn, cash flow, capital invested, ROIC.

**AI inference is a line item.** Cost is tracked per agent / business / task / model so uneconomic agents are visible.

## Security

Least privilege, RBAC, secret management, per-business credential isolation, encryption, audit logging, agent permission boundaries, rate and spending limits, human approval gates, business isolation, backup/recovery, monitoring/alerting.

## Technology

Modular, API-driven. Python + FastAPI backend, PostgreSQL, Redis, Celery-style workers, Docker. Multiple LLM providers — never tightly coupled to one. MCP-compatible integrations where useful. Kubernetes only when real demand requires it.