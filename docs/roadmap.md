# Roadmap

## Phase 1 — Business Factory MVP

Goal: launch **one real business** through the system end to end.

Build:

- [ ] Orchestrator core: task scheduling, agent registry, run tracking
- [ ] Agent framework: base agent class, typed tasks, structured outputs, budgets
- [ ] PostgreSQL schema: businesses, opportunities, experiments, ledger, approvals, audit logs
- [ ] Business registry: CRUD + state machine (DISCOVERED → … → OPERATING)
- [ ] Opportunity pipeline: discovery → research report → scoring engine
- [ ] Scoring engine: configurable weights across demand, competition, margin, cost, complexity, automation potential, scalability
- [ ] Experiment engine: fixed budgets/durations/targets, evaluation, SCALE/PAUSE/SHUT DOWN recommendations
- [ ] Financial ledger: per-business P&L, contribution profit, unit economics
- [ ] Approval system: gates, thresholds, human review queue
- [ ] Audit logging: every agent action recorded with cost and result
- [ ] Basic dashboard: portfolio view, business drill-down, experiment status, approval queue

**Exit criterion:** one legitimate business launched through the platform generates its first $1,000 in real customer revenue with positive unit economics.

## Phase 2 — Operations

- [ ] Marketing agents: SEO, paid search/social, email, content, affiliates — all budget-capped
- [ ] Customer service agent: FAQs, order/shipping questions, refund triage with human escalation rules
- [ ] Creative agent: product images, ad concepts, copy — versioned and tied to campaign performance
- [ ] Analytics: traffic, conversion funnels, cohort reporting
- [ ] Financial reporting: scheduled P&L, cash-flow views
- [ ] Operational monitoring: orders, inventory, uptime, payment failures, alerting
- [ ] External integrations: first commerce adapter (e.g. Shopify or Etsy), email provider, ad platform

**Exit criterion:** the first business runs day-to-day with minimal human touch; marketing spend stays within approved budgets.

## Phase 3 — Portfolio Management

- [ ] Multiple simultaneous businesses under one control plane
- [ ] Capital allocation engine: rank businesses by capital efficiency (net profit / capital deployed), recommend SCALE / MAINTAIN / REDUCE / PAUSE / SHUT DOWN
- [ ] Cross-business analytics: shared learnings, benchmark comparisons
- [ ] Automated experimentation: parallel experiments with portfolio-level budget caps
- [ ] Business shutdown workflows: graceful termination, asset archival, data retention for learning

**Exit criterion:** 3+ businesses operating simultaneously; capital allocation recommendations demonstrably beating naive equal-split.

## Phase 4 — Advanced Intelligence

- [ ] Agent memory: historical experiments, failures, campaign performance, supplier track records
- [ ] Predictive models: opportunity ranking from accumulated data
- [ ] Dynamic pricing experiments
- [ ] Marketing optimization: budget reallocation from measured ROAS
- [ ] Anomaly detection: automated alerts on metric deviations
- [ ] Model routing + AI cost optimization: cheapest capable model per task

**Exit criterion:** agent decisions measurably improve with accumulated history (e.g. scoring model calibrated against real outcomes).

## Phase 5 — Scale

- [ ] Infrastructure scale-out only when real operating demand requires it
- [ ] No premature distributed systems — Docker Compose until it hurts, then Kubernetes

**Exit criterion:** the $60K/mo net profit target, or a documented reason the target moved.

## Standing rules

- Do not build phases in parallel. Each phase's exit criterion gates the next.
- Revenue alone never determines success — contribution profit and unit economics decide.
- Every phase must keep: audit logging, approval gates, per-business isolation, AI cost tracking.
