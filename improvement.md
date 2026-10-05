# Improvement Plan — Multi-Vertical Expansion

**Status:** Draft for owner review
**Date:** 2026-10-05
**Scope:** Generalize the platform from one hardcoded vertical (Etsy digital
downloads) to a plugin model that can host dropshipping, print-on-demand, and
micro-SaaS without forking the orchestrator, the ledger, or the agents.

> This plan is **architecture work, not a launch authorization.** See
> [Standing constraint](#0-standing-constraint) before spending anything.

---

## 0. Standing constraint

`docs/roadmap.md` sets the rule: *"Do not build phases in parallel. Each
phase's exit criterion gates the next."* Phase 1's exit criterion is **one
business generating $1,000 in real revenue with positive unit economics.**

As of today, Experiment 001 is at **$0 revenue, day 2 of 60**, with 12 Etsy
listings still in draft awaiting owner activation. Phase 1 is not met.

So the sequencing rule for everything below:

| Work | Gate |
|------|------|
| Stage A (prerequisite fixes) | None — do now, it protects Experiment 001 |
| Stages B–D (abstraction, refactor, economics) | None — pure refactor, no spend |
| Stage E+ (new adapters, new verticals) | Build behind a flag, **no real money** |
| **Launching a second real-money experiment** | **Experiment 001 resolves** (hits $1K, or hits a stop-loss and is documented) |

Building the abstraction now is right — doing it while Experiment 001 is live
and cheap is the ideal time. **Funding a second vertical now is not.** One
unresolved experiment is a sample size of zero.

---

## 1. Why the current code cannot host a second vertical

Six of the defects in the inspection report are not merely bugs — they are
load-bearing assumptions that only hold because there is exactly one vertical,
and that vertical has ~88% margins that hide arithmetic errors.

| Defect | Harmless today | Why it breaks multi-vertical |
|--------|----------------|------------------------------|
| AI cost never reaches `AgentRun` (`orchestrator/engine.py:106`) | Digital margin absorbs it | Dropshipping nets ~$3/order. Untracked AI spend **is** the margin. Per-vertical AI cost attribution is the whole point of the invariant. |
| `"ai_spend"` vs `"ai_inference"` kind mismatch (`ecosystem/worker.py:126`) | Lands in `other`, nobody looks | Thin-margin verticals need every cost line correct to decide scale-vs-kill |
| Allocation over-allocates 2× the pool (`core/allocation.py:81`) | One business, no contention | Cross-vertical capital routing is the entire Phase 3 thesis |
| `evaluate_all()` marks every experiment SHUT DOWN and frees the cap (`core/portfolio_experiments.py:50`) | Never called with >1 experiment | Parallel verticals = parallel experiments = this fires |
| Cannot shut down from early states (`core/shutdown.py:45`) | Experiment 001 is past `APPROVED` | Dropshipping fails *early and often*; `DISCOVERED`→`TERMINATED` is the common path |
| Stop-loss precedence bug (`ecosystem/etsy_monitor.py:211`) | One monitor, one bug | Every new vertical copy-pastes this monitor and inherits it |

There is also one structural problem that is not a bug yet:

**`ScoringEngine` has a single global weight set.** `core/scoring.py:16-28`
weights `expected_margin` at 0.15 and `startup_cost` at 0.08 for everything. A
20%-margin dropshipping opportunity is not "worse" than an 88%-margin digital
product — it is a *different asset class* with different capital velocity and
different risk. Ranking them on one weight vector will systematically starve
every vertical except digital downloads, and the scorer will look like it is
working while doing it.

**Conclusion:** Stage A is not optional cleanup. It is the foundation.

---

## 2. Vertical selection

Assessed against the constraints that actually bind this platform: low capital,
agent-operable, human holds the money, 60-day experiment cycle, measurable unit
economics.

| Vertical | Capital | Margin | Agent-operable | Time to first $ | Verdict |
|----------|---------|--------|----------------|-----------------|---------|
| **Print-on-demand** | $50–150 | 25–40% | High — listing, copy, SEO, design briefs | 2–4 wks | **Build first** |
| **Dropshipping** | $500–1,500 | 10–25% | Medium — needs paid traffic + supplier mgmt | 1–3 wks | **Build second, with guardrails** |
| **Micro-SaaS / paid API** | $100–400 | 80–95% | Medium — needs real product work | 4–12 wks | **Build third** |
| Affiliate / content SEO | $50–200 | ~95% | High | 3–9 **months** | Defer — ramp exceeds the 60-day cycle |
| B2B lead generation | $300–800 | 50–70% | Low — human relationship sales | 4–8 wks | Defer — poor agent fit |
| Digital products, other marketplaces (Gumroad, Creative Market) | $0–50 | 85–95% | High | 1–3 wks | Trivial horizontal copy; do it opportunistically, not as a vertical |

### Why print-on-demand before dropshipping

You asked about dropshipping specifically. It is feasible, and it is planned
below in full — but it is the **worst risk-adjusted first expansion**, and PoD
is strictly the better bridge. Concretely:

- **PoD reuses the Etsy adapter you already paid for.** Printful/Printify sit
  *behind* an Etsy or Shopify storefront. The OAuth work, the listing flow, the
  `EtsyCommerceAdapter` — all reused.
- **PoD introduces COGS and fulfillment without supplier capital risk.** You
  learn to model per-order cost, shipping, and production SLA — the exact
  muscles dropshipping needs — while nobody prepays a supplier.
- **PoD has organic traffic.** Etsy search supplies demand. Dropshipping does
  not: it is a **paid-acquisition business**, which means CAC is the primary
  risk and ad spend is the primary cost. That inverts the approval model —
  `ApprovalPolicy.ad_spend_auto_max` is $50, and dropshipping validation
  typically needs $300–800 of ad testing to find a winner. **Every dropshipping
  experiment will sit in the human approval queue continuously.**
- **Dropshipping carries risk classes this platform has no concept of:**
  payment-processor rolling reserves, ad-account bans, chargeback-rate
  thresholds that terminate merchant accounts, counterfeit/trademark exposure on
  imported catalogs, and customs/duties on cross-border orders.

My recommendation: **PoD as vertical #2, dropshipping as #3.** If you want
dropshipping first regardless, Stage F.3 below is the risk-control work that
has to land with it — not after.

---

## 3. Target architecture: the `Vertical` plugin

One new concept. A `Vertical` owns everything that differs between business
models, so the orchestrator, ledger, agents, and dashboard stay vertical-blind.

```
core/verticals/
├── base.py               Vertical ABC, Charter, UnitEconomics, StopLossRule
├── registry.py           slug -> Vertical lookup
├── digital_downloads.py  <- extracted from today's hardcoded Etsy behavior
├── print_on_demand.py
├── dropshipping.py
└── micro_saas.py
```

### 3.1 The ABC

```python
# core/verticals/base.py
class Vertical(ABC):
    slug: str                          # "dropshipping"
    display_name: str

    # Scoring: each vertical ranks opportunities on its own weight vector.
    @abstractmethod
    def scoring_weights(self) -> ScoringWeights: ...

    # Per-order economics. THE critical method — see section 5.
    @abstractmethod
    def unit_economics(self, inputs: OrderInputs) -> UnitEconomics: ...

    # Default experiment charter for a given capital level.
    @abstractmethod
    def default_charter(self, capital_usd: float, launch: date) -> Charter: ...

    # Ledger kinds this vertical is allowed to emit (validated at record time).
    ledger_kinds: frozenset[str] = frozenset()

    # Adapter types that must be registered before launch is permitted.
    required_adapters: tuple[type, ...] = ()

    # Vertical-specific approval overrides, merged onto the base policy.
    def approval_overrides(self) -> dict: return {}

    # Named risk factors, surfaced on the dashboard and in the charter.
    @abstractmethod
    def risk_factors(self) -> list[RiskFactor]: ...
```

### 3.2 Charter becomes data, not module constants

Today the Experiment 001 charter lives as **module-level globals** in
`ecosystem/etsy_monitor.py:34-42` (`LAUNCH_DATE`, `TARGET_REVENUE_USD`,
`TOTAL_BUDGET_USD`, `BASELINE_SPEND_USD`). That is why there can only be one
experiment, and why `test_fresh_launch_is_on_track` is a time bomb.

```python
class Charter(BaseModel):
    experiment_id: str
    vertical: str
    launch_date: date
    duration_days: int
    target_revenue_usd: float
    target_orders: int
    budget_lines: dict[str, float]      # {"ads": 50.0, "tools": 26.0, ...}
    total_budget_usd: float
    kpi_targets: dict[str, float]       # cac_max, conversion_min, ...
    stop_loss_rules: list[StopLossRule]
```

### 3.3 Stop-loss rules become objects

This is the structural fix for report finding #3. The day-30 stop-loss failed
because it was inline boolean soup with an operator-precedence bug
(`and` binding tighter than `or`), silently unreachable, and untestable in
isolation. Replicating that pattern across four verticals would be four copies
of the same class of bug.

```python
class StopLossRule(ABC):
    id: str
    description: str

    @abstractmethod
    def evaluate(self, snapshot: MetricsSnapshot) -> str | None:
        """Return a human-readable trigger reason, or None if not tripped."""
```

Concrete rules, each independently unit-tested:

```python
class BudgetExhaustedWithoutRevenue(StopLossRule): ...   # spend >= cap AND revenue < floor
class NoGrowthAtCheckpoint(StopLossRule): ...            # day N: revenue < X AND wow flat-or-declining
class NegativeContributionAfterNOrders(StopLossRule): ...
class ChargebackRateCeiling(StopLossRule): ...           # dropshipping-specific
class CACExceedsLTV(StopLossRule): ...                   # paid-acquisition verticals
class SupplierSLABreach(StopLossRule): ...               # dropshipping / PoD
```

`NoGrowthAtCheckpoint.evaluate()` gets the corrected logic once, with a test
table covering `[0,0,0]`, `[10,50,150]`, `[5,5,5]`, `[100,50,10]` — the exact
cases that currently misbehave — and every vertical inherits the fix.

---

## 4. Stage-by-stage plan

Estimates are focused-work days for one developer, excluding review.

### Stage A — Prerequisite fixes (gate for everything else) · ~2 days

Not the full defect list from the inspection report — only what blocks
multi-vertical work or protects the live experiment.

| # | Fix | File |
|---|-----|------|
| A1 | Copy `handler.tokens_used` / `cost_usd` onto `AgentRun` in `dispatch()` | `orchestrator/engine.py:106-121` |
| A2 | `"ai_spend"` → `"ai_inference"`; validate `kind` against `KIND_TO_FIELD` and raise on unknown | `ecosystem/worker.py:126`, `core/ledger.py:72` |
| A3 | Fix day-30 stop-loss precedence (parenthesize) — **do this first, it guards live money** | `ecosystem/etsy_monitor.py:211` |
| A4 | Reject negative `budget_usd` / `budget_tokens` in `submit()` | `orchestrator/engine.py:65` |
| A5 | Cap allocation at the pool; drop the floor when it would over-commit | `core/allocation.py:81` |
| A6 | Route `shutdown()` through direct `TERMINATED` transition when `PAUSED` is unreachable | `core/shutdown.py:45` |
| A7 | Make `evaluate_all()` non-destructive: add `ExperimentEngine.assess()` (read-only) and require explicit actuals to *complete* an experiment | `core/experiments.py:54`, `core/portfolio_experiments.py:50` |
| A8 | Replace `→` with `->` (worker crashes on Windows cp1252) | `ecosystem/worker.py:183` + 3 others |
| A9 | Add `ecosystem*` to `packages.find.include`; add `python-multipart` to `requirements.txt` | `pyproject.toml:30`, `requirements.txt` |
| A10 | Pin `today` in `test_fresh_launch_is_on_track` | `tests/test_etsy_monitor.py:40` |

**Exit criterion:** 45/45 tests pass; `python launch.py worker -- --ticks 3`
runs clean on Windows and Fedora; a tick records nonzero `ai_inference` in the
P&L; `pip install .` (non-editable) into a clean venv runs `launch.py`.

---

### Stage B — Vertical abstraction, no new verticals · ~3 days

Build the plugin machinery and prove it by **refactoring what exists** — not by
adding anything.

- B1. Write `core/verticals/base.py` (`Vertical`, `Charter`, `UnitEconomics`,
  `OrderInputs`, `StopLossRule`, `RiskFactor`, `MetricsSnapshot`).
- B2. Write `core/verticals/registry.py`.
- B3. Write `core/verticals/digital_downloads.py` — move the Experiment 001
  charter constants, the three stop-loss rules, and the $12-planner unit
  economics out of `etsy_monitor.py` into this class.
- B4. Add `vertical: str` to `Business` and `Experiment`, validated against the
  registry. Keep `business_type` as a free-text label for now; do **not** do a
  breaking rename while Experiment 001 is live.
- B5. `ScoringEngine.__init__` takes weights from a `Vertical`; `score_and_rank`
  gains a `vertical` argument. Cross-vertical comparison uses risk-adjusted
  score (see G2), never raw score.

**Exit criterion:** `digital_downloads` is the only registered vertical, every
existing test still passes unchanged, and `ecosystem/etsy_monitor.py` contains
zero charter constants.

---

### Stage C — Generalize the experiment monitor · ~2 days

- C1. New `ecosystem/experiment_monitor.py`: vertical-agnostic. Takes a
  `Charter` + a `MetricsSource`, produces a `MetricsSnapshot`, evaluates
  `charter.stop_loss_rules`, returns the verdict. All the orchestration logic
  currently inside `EtsyMonitor.check()`.
- C2. Define `MetricsSource` protocol: `fetch(self) -> MetricsSnapshot`.
  `EtsyMonitor` becomes `EtsyMetricsSource`, keeping only the API reads and
  spend bookkeeping.
- C3. Persist charters and snapshots. Today spend lives in a JSON file at
  `~/.config/evergreen-etsy/spend.json` and revenue deltas in
  `monitor_state.json` — workable for one experiment, unworkable for N.
- C4. Dashboard: replace the hardcoded `/experiment-001` route with
  `/experiments/{id}/monitor`, rendered from the generic snapshot. Keep
  `/experiment-001` as a redirect so nothing the owner has bookmarked breaks.

**Exit criterion:** Experiment 001 monitoring is byte-identical in output,
driven entirely by a persisted `Charter`; a second charter can be registered and
monitored with zero code changes.

---

### Stage D — Unit economics engine · ~2 days

The intellectual core. See section 5 for per-vertical models.

- D1. `UnitEconomics` model: per-order revenue, itemized costs, contribution
  margin, **and CAC-inclusive contribution** — the figure that actually decides
  paid-acquisition verticals.
- D2. `Vertical.unit_economics()` for each vertical.
- D3. Wire into `ExperimentEngine.evaluate()` so the SCALE/OPTIMIZE/PAUSE/SHUT
  DOWN decision reads real per-order economics instead of aggregate
  `revenue − operating_cost`.
- D4. Surface per-order economics on the business drill-down page — and while
  there, **actually render the audit event rows** (`dashboard/app.py:104`
  currently prints the count and no rows).

**Exit criterion:** For each vertical, a $0-revenue experiment and a
break-even experiment both produce correct, hand-checkable per-order figures.

---

### Stage E — Adapter layer expansion · ~4 days

`core/integrations.py` has `CommerceAdapter`. Three more ABCs are needed.

```python
class FulfillmentAdapter(ABC):       # Printful, Printify, CJ, supplier APIs
    def quote(self, sku, destination) -> FulfillmentQuote
    def submit_order(self, order) -> FulfillmentOrder
    def track(self, fulfillment_id) -> ShipmentStatus
    def production_sla_days(self, sku) -> int

class AdPlatformAdapter(ABC):        # Meta, TikTok, Google, Etsy Ads
    def create_campaign(self, spec) -> PlatformCampaign
    def spend_to_date(self, campaign_id) -> float
    def metrics(self, campaign_id) -> AdMetrics      # impressions, clicks, conv, CAC
    def pause(self, campaign_id) -> None             # must be callable by stop-loss

class PaymentAdapter(ABC):           # Stripe, Shopify Payments, Etsy Payments
    def settlement_summary(self, since) -> Settlement
    def chargeback_rate(self, window_days) -> float
    def reserve_held_usd(self) -> float
```

- E1. **Fix the Etsy OAuth PKCE flow first** (report finding #8:
  `code_challenge` sends the raw verifier under an `S256` declaration, and the
  token request uses `code_challenge` where the spec requires `code_verifier`).
  Shopify OAuth will be written by analogy to this file — fix it before it is
  copied. Add the adapter test coverage it currently entirely lacks.
- E2. `PrintfulAdapter` (`FulfillmentAdapter`) — PoD.
- E3. `ShopifyAdapter` (`CommerceAdapter`) — dropshipping storefront.
- E4. `MetaAdsAdapter` (`AdPlatformAdapter`) — dropshipping traffic.
- E5. `StripeAdapter` (`PaymentAdapter`) — chargeback rate and reserve
  visibility; also the billing layer for micro-SaaS.
- E6. `AdapterRegistry` gains `require(business_id, vertical)` — refuse to
  transition a business to `LAUNCHED` unless every `vertical.required_adapters`
  entry is registered.

**Exit criterion:** every adapter has a mock implementation plus a conformance
test; no vertical can reach `LAUNCHED` with a missing adapter.

---

### Stage F — Vertical implementations

#### F.1 Print-on-demand · ~3 days

- Vertical class: weights favor `automation_potential` and `scalability`,
  tolerate 25–40% margin, penalize `supplier_risk` hard (single print partner).
- Economics: base cost + shipping + marketplace fee + processing.
- Stop-loss: standard three, plus `SupplierSLABreach` (production exceeding
  quoted SLA destroys Etsy review scores — and reviews *are* the traffic).
- Agents: `CreativeAgent` gains design briefs with print-spec constraints
  (DPI, bleed, mockup requirements). `OperationsAgent` monitors production SLA.
- Reuses the Etsy adapter and the existing listing/SEO flows wholesale.

#### F.2 Dropshipping · ~5 days

- Vertical class: weights heavily penalize `supplier_risk`, `marketplace_risk`,
  `support_burden`; `advertising_cost` becomes a top-3 weight, not 0.06.
- Economics: product cost + inbound shipping + **ad spend as a per-order cost**
  + processing + refund allowance + chargeback allowance. See section 5.3.
- **New agent — `SupplierAgent`** (`agents/supplier/agent.py`): vet suppliers,
  track on-time rate and defect rate, recommend switching. This finally uses
  `AgentMemory.supplier_rating()` (`core/memory.py:89`), which exists today and
  has **zero callers**.
- `MarketingAgent` gets real platform wiring. Today `create_campaign`/`spend`
  (`agents/marketing/agent.py:50-81`) are internal bookkeeping with no
  connection to any ad platform — the budget cap is enforced against a number in
  a dict, not against actual spend. For dropshipping that gap is the whole risk.
- `SupportAgent` gets refund/chargeback wiring through `PaymentAdapter`. The
  $100 escalation threshold exists (`agents/support/agent.py:46`) but nothing
  feeds it real orders.

#### F.3 Dropshipping risk controls — **ships with F.2, not after** · ~2 days

Risk classes the platform currently cannot represent:

| Risk | Control |
|------|---------|
| Chargeback rate terminates the merchant account | `ChargebackRateCeiling` stop-loss at 0.65% (well under the ~1% processor threshold) |
| Payment processor rolling reserve traps cash | `PaymentAdapter.reserve_held_usd()` excluded from available capital in allocation |
| Ad account ban kills all traffic overnight | `required_adapters` permits a second ad platform; `RiskFactor` surfaced on the dashboard |
| Counterfeit / trademark exposure on imported catalogs | New always-require approval: `import_supplier_catalog`. **Agents must never auto-list a third-party catalog.** |
| Customs / duties on cross-border orders | Per-destination cost line in `unit_economics` |
| Long shipping times drive refunds | `SupplierSLABreach` + refund allowance in the economics model |

#### F.4 Micro-SaaS · ~4 days

- Economics: hosting + API cost + AI inference + processing against MRR;
  churn-adjusted LTV. The existing ledger kinds already cover this cleanly.
- `StripeAdapter` for subscription billing; `recurring_revenue` weight goes to
  the top of the vector.
- Stop-loss: `CACExceedsLTV`, plus a churn ceiling.
- Honest note: this vertical needs actual software to sell. The platform can
  research, price, market, and support it, but something has to build the
  product. Scope that as human work or a separate build agent — do not pretend
  the current agent set can ship a SaaS.

---

### Stage G — Cross-vertical portfolio · ~3 days

- G1. `PortfolioAnalytics.benchmarks()` segments by vertical. Comparing an 88%
  digital margin against a 15% dropship margin as peers is meaningless; also
  fix the `median` on even-length lists (`core/portfolio.py:37` takes the upper
  element).
- G2. **Risk-adjusted allocation.** `CapitalAllocator` currently ranks on raw
  `net_profit / capital_deployed`. Add a vertical risk multiplier so capital
  velocity and failure rate are priced in, and dropshipping's high gross
  throughput does not out-rank digital's durable margin on a raw ratio.
- G3. **Vertical concentration limits.** Add a cap (default 50%) on the share of
  portfolio capital in any one vertical. Diversification is the README's stated
  objective — nothing currently enforces it.
- G4. Dashboard: portfolio view grouped by vertical, with per-vertical P&L.

---

### Stage H — Approval policy for the new verticals · ~1 day

Add to `ApprovalPolicy.always_require` (`orchestrator/approvals.py:19`):

```
import_supplier_catalog      # IP/counterfeit exposure
add_supplier                 # new counterparty
connect_ad_account           # new spend channel
connect_payment_processor    # new money path
raise_vertical_concentration # breach the diversification cap
launch_new_vertical          # first real-money experiment in a vertical
```

And — flagged plainly — **the approval queue has no authentication** and binds
to `0.0.0.0` by default (report finding #12). Adding dropshipping means the
unauthenticated queue starts gating supplier onboarding and ad-account
connection. **Auth belongs in Stage A, not Stage H,** if any of this will run on
a network anyone else can reach.

---

## 5. Unit economics models

> **Illustrative structure, not researched figures.** Every number below must be
> replaced with verified market data before any spend, per the charter rule:
> *no charter, no spend.* `docs/experiments/001-etsy-digital-downloads.md` is the
> standard to match — it did this work properly.

### 5.1 Digital downloads (current, for reference)

```
$12.00  revenue
-$0.78  transaction (6.5%)
-$0.61  processing (3% + $0.25)
-$0.02  listing fee amortized
-------
+$10.59 contribution   (~88%)
```

No COGS, no shipping, no fulfillment risk. This is why arithmetic bugs are
invisible here.

### 5.2 Print-on-demand

```
$24.00  revenue
-$9.50  base product cost
-$4.50  shipping
-$1.56  marketplace fee (6.5%)
-$0.97  processing
-------
+$7.47  contribution   (~31%)
```

New risk: production SLA. A 10-day print delay costs a review, and on Etsy
reviews are the traffic.

### 5.3 Dropshipping — note the CAC line

```
$34.99  revenue
-$8.00  product cost
-$4.00  inbound shipping
-$1.31  processing (2.9% + $0.30)
-$1.05  refund allowance (3%)
-$0.23  chargeback allowance (0.65%)
-------
+$20.40 gross contribution   (~58%)

-$18.00 CAC (paid acquisition — the actual constraint)
-------
+$2.40  net contribution per order   (~7%)
```

**The whole vertical lives or dies on that last line.** At a $20 CAC this
business is underwater, and no supplier negotiation saves it. Which is why
`CACExceedsLTV` is a stop-loss rule and not a dashboard metric, and why
`advertising_cost` has to be a top-weighted scoring factor rather than 0.06.

### 5.4 Micro-SaaS

```
$29.00  MRR per customer
-$0.84  processing
-$2.00  hosting + API
-$1.50  AI inference
-------
+$24.66 monthly contribution   (~85%)
x  14   avg months (7% monthly churn)
-------
 $345   LTV   ->   CAC ceiling ~$115 at 3:1
```

---

## 6. Testing strategy

The inspection found tests are **module-shaped, not behavior-shaped** — which is
exactly why six cross-module defects survived a suite where every module's own
tests passed. Do not replicate that across four verticals.

- **T1. Vertical conformance suite.** `tests/test_vertical_conformance.py`,
  parametrized over the registry. Every vertical must: produce valid scoring
  weights summing to 1.0; emit only declared `ledger_kinds`; produce a charter
  whose budget lines sum to `total_budget_usd`; compute unit economics that
  balance; declare at least one stop-loss rule; name its risk factors. A new
  vertical is "done" when it passes this unchanged.
- **T2. Stop-loss rule tests.** Each rule tested standalone against a snapshot
  table, including the boundary cases the current inline logic gets wrong.
- **T3. Cross-module integration tests per vertical.** Launch → spend → monitor
  → stop-loss → shutdown, end to end against mock adapters. `test_launch.py` and
  `test_hardening.py` are the only tests that cross boundaries today, and they
  are the only ones that caught real bugs. Extend that pattern.
- **T4. Adapter contract tests.** One suite run against both the mock and
  (manually, gated) the live adapter.
- **T5. Economics golden tests.** Hand-computed expected values per vertical, so
  a weight or fee change that silently breaks margin math fails loudly.
- **T6. Determinism.** Replace `hash()` in `HeuristicResearchSource`
  (`agents/research/agent.py:53`) with a stable digest. `PYTHONHASHSEED`
  randomization currently makes research verdicts differ every process start,
  while the docstring claims determinism.

---

## 7. Sequencing and effort

| Stage | Work | Days | Gate |
|-------|------|-----:|------|
| A | Prerequisite fixes | 2 | — |
| B | Vertical abstraction + refactor | 3 | A |
| C | Generic experiment monitor | 2 | B |
| D | Unit economics engine | 2 | B |
| E | Adapter layer (+ fix Etsy PKCE) | 4 | A |
| F.1 | Print-on-demand | 3 | C, D, E |
| F.2 | Dropshipping | 5 | F.1 |
| F.3 | Dropshipping risk controls | 2 | ships with F.2 |
| F.4 | Micro-SaaS | 4 | F.1 |
| G | Cross-vertical portfolio | 3 | two live verticals |
| H | Approval policy | 1 | F.2 |
| | **Total** | **~31** | |

Natural checkpoints:

- **After A** (~2 days) — Experiment 001's stop-loss actually works and AI cost
  is tracked. Highest value per hour in the whole plan. Do this regardless of
  whether the rest proceeds.
- **After C+D** (~9 days) — the platform is vertical-agnostic with one vertical
  registered. Nothing new launched, nothing spent, and the hard refactor is done
  while the stakes are still zero.
- **After F.1** (~16 days) — PoD is buildable. **Launching it with real money
  still requires Experiment 001 to have resolved.**

---

## 8. What not to build

- **No new real-money experiment until Experiment 001 resolves.** The roadmap's
  own standing rule. One unresolved experiment is a sample size of zero, and the
  $60K objective depends on knowing whether the *first* loop closes.
- **No Kubernetes, no Celery, no distributed workers.** `docs/roadmap.md` Phase 5
  is explicit: "Docker Compose until it hurts." It does not hurt yet.
- **No LLM integration as part of this plan.** There is currently no model
  provider anywhere in the codebase — every agent is a heuristic returning
  hardcoded data and every "AI cost" is a literal constant. That is a defensible
  scaffold, but it is a *separate* workstream from multi-vertical support, and
  conflating them will produce neither. Do the abstraction against the
  heuristics; swap in real inference behind the `OpportunitySource` /
  `ResearchSource` protocols that already exist for exactly this purpose.
- **No `business_type` → `vertical` breaking rename while Experiment 001 is
  live.** Add the field, dual-write, migrate later.
- **Do not generalize the Postgres layer in this plan.** Businesses, ledger,
  approvals, and audit are in-memory only — 8 tables are defined in
  `infra/db_models.py` and 6 have zero writers. That is a real gap (a
  non-durable audit log under `Restart=always` contradicts the auditability
  invariant), but it is orthogonal to vertical support. Separate workstream,
  and worth doing before anything carries meaningful money.

---

## 9. Open questions for the owner

1. **PoD before dropshipping, or dropshipping first?** My recommendation is PoD
   (section 2). If dropshipping goes first, F.3 ships with it, not after.
2. **Ad spend approval tiers.** Dropshipping validation needs $300–800 of
   testing; the current auto-tier is $50/day. Does the policy change, or does
   every dropshipping experiment live in the approval queue? Both are
   defensible — but it has to be a decision, not a surprise.
3. **Who builds the micro-SaaS product?** The platform can research, price,
   market, and support it. It cannot currently build it.
4. **Does the dashboard get authentication before anything runs on a reachable
   network?** This gates Stage H either way.
