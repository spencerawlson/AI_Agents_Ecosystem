# Shopify store pipeline

Trends → product briefs → supplier report → **owner approves a supplier** →
product live → paused ads → **owner approves the launch** → live ads →
spend guard, profit tracking, SEO and content every cycle.

| Piece | File |
|-------|------|
| Trend-checked product discovery | `core/product_discovery.py` |
| Supplier model, scoring, report | `core/sourcing.py` |
| Automated supplier search (CJ Dropshipping API) | `core/supplier_search.py` |
| Margin pricing (60% after cost, shipping, payment fee) | `core/pricing.py` |
| Shopify Admin GraphQL adapter | `core/shopify_adapter.py` |
| Meta + Google Ads adapters, ad copy | `core/ads.py` |
| Ad drafting + launch/budget approvals | `ecosystem/ads_pipeline.py` |
| Spend guard, profit, SEO, blog drafts, store report | `ecosystem/store_ops.py` |
| CLI | `ecosystem/shopify_pipeline.py` |

## What needs your approval (and what doesn't)

| Action | Who |
|--------|-----|
| Pick a supplier → product goes live at the 60%-margin price | **You** (`approve_supplier`) |
| Start spending on an ad campaign | **You** (`launch_ad_campaign`, amount = daily × days) |
| Raise a winning campaign's budget | **You** (`ad_budget_increase`) |
| Build campaigns (paused, $0 spend) | Agents |
| Pause a campaign that is losing money or used its budget | Agents (pausing only stops spend) |
| Fix SEO title / meta description | Agents (search-only fields) |
| Write blog articles | Agents, created **hidden** — you make them visible in Shopify |
| Product title, description, price after launch | Never changed automatically |

## Connect accounts (once)

All credentials go in environment variables — never in the repo.
`python ecosystem/shopify_pipeline.py check` reports what is connected.

**Shopify** — create an app in the Shopify Dev Dashboard (or a legacy custom app)
with scopes `read_products write_products read_inventory write_inventory
read_locations read_publications write_publications read_orders read_content
write_content`, install it, then set:

```
SHOPIFY_SHOP=your-store.myshopify.com
SHOPIFY_ACCESS_TOKEN=shpat_...                     # or:
SHOPIFY_CLIENT_ID=...  SHOPIFY_CLIENT_SECRET=...   # client-credentials grant
```

**Supplier search (optional)** — Alibaba has no public buyer search API, so
automated search uses CJ Dropshipping (free account → Authorization → API key):
`CJ_API_KEY=...`. Alibaba stays manual: every supplier report has Alibaba search
links, and quotes you add to the brief's `suppliers` list are ranked alongside CJ.

**Meta ads (optional)** — Business Manager system user with `ads_management`:
`META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`, `META_PAGE_ID`, `META_PIXEL_ID`
(pixel connected to the Shopify store so purchases are tracked).

**Google ads (optional)** — `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CLIENT_ID`,
`GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_REFRESH_TOKEN`, `GOOGLE_ADS_CUSTOMER_ID`,
optionally `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. Conversion tracking must be set up
(Google & YouTube Shopify app) for the spend guard to see sales.

**Ad defaults (optional)** — `ADS_DAILY_BUDGET_USD` (10), `ADS_DURATION_DAYS` (7),
`ADS_COUNTRIES` (`US`), `ADS_AUTO_DRAFT` (`true`).

**LLM (optional, recommended)** — the existing gateway key enables idea
generation, listing copy, ad copy and blog articles. Without it, templates
are used and discovery needs a `--seeds` file.

## Run it

```
# 1. Find products (LLM ideas or your own seed list), check demand, source suppliers
python ecosystem/shopify_pipeline.py discover --niche "home office" --llm --source
#    or for one brief you wrote:  ... source my_brief.json --llm

# 2. Read reports/supplier_report_*.md, contact suppliers, approve ONE in /approvals

# 3. Run the worker (or `... cycle` by hand). Each tick it publishes approved
#    products, builds paused ads for them and launches the ones you approve.
python ecosystem/worker.py --store-optimize-every 30
```

Every `--store-optimize-every` ticks the worker also runs the optimisation
pass and writes `reports/store_report_<date>.md`: profit per product (net of
ad spend), campaign results against the cost-per-sale ceiling, actions
taken, SEO issues and fixes, blog drafts.

## Pricing and the spend guard

`price = (landed_cost + $0.30) / (1 − margin − 2.9%)`, rounded up to `.99`.
Ad spend is not in the price. The **cost-per-sale ceiling** is the most an
order can spend on ads and still keep a 20% net margin. The guard pauses a
campaign when:

- its approved budget is spent, or
- it spent 2× the ceiling with no sale, or
- after 3+ sales its cost per sale is above 1.25× the ceiling.

After 3+ sales at ≤ 70% of the ceiling, it asks you to raise the daily budget 1.5×.

## Known limits

- The Shopify, CJ, Meta and Google adapters are written against each API's
  documentation and tested against stand-ins, not live accounts. Run one
  product end to end with small budgets first. API versions are configurable
  (`SHOPIFY_API_VERSION`, `META_API_VERSION`, `GOOGLE_ADS_API_VERSION`).
- Google Trends goes through `pytrends` (in `requirements.txt`). Without it,
  or when Google rate-limits, ideas are flagged "demand not verified" rather
  than dropped.
- Profit ignores refunds issued after payment, chargebacks and app fees.
  Fully refunded/voided orders are excluded.
- Orders are not placed with the supplier automatically. Fulfil through CJ's
  Shopify app or your supplier's process.
