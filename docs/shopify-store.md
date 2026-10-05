# Shopify store pipeline

Brief → supplier report → **owner approval** → product live on Shopify.

| Piece | File |
|-------|------|
| Margin pricing (60% after cost, shipping, payment fee) | `core/pricing.py` |
| Supplier model, scoring, report | `core/sourcing.py` |
| Shopify Admin GraphQL adapter | `core/shopify_adapter.py` |
| Pipeline CLI (`check`, `source`, `publish-approved`) | `ecosystem/shopify_pipeline.py` |
| Example brief | `examples/shopify_brief.example.json` |

## Connect the store (once)

1. Create an app for your store in the Shopify **Dev Dashboard** (or a legacy
   custom app in the store admin) with these Admin API scopes:
   `read_products write_products read_inventory write_inventory
   read_locations read_publications write_publications read_orders`.
   No order-editing, refund or payment scopes — those stay human-only.
2. Install the app on the store and set credentials in the environment
   (never commit them):

   ```
   SHOPIFY_SHOP=your-store.myshopify.com
   SHOPIFY_ACCESS_TOKEN=shpat_...            # static token, or:
   SHOPIFY_CLIENT_ID=...  SHOPIFY_CLIENT_SECRET=...   # client-credentials grant
   SHOPIFY_API_VERSION=2026-07               # optional
   ```

3. Verify: `python ecosystem/shopify_pipeline.py check`

## Run a product

1. Copy `examples/shopify_brief.example.json`, fill in the niche, keywords,
   market price band and supplier candidates (unit cost, per-order shipping,
   MOQ, delivery days, rating, image URLs).
2. `python ecosystem/shopify_pipeline.py source my_brief.json --llm`
   writes `reports/supplier_report_<brief>_<date>.md` and opens one approval
   per supplier. Nothing is published.
3. Contact suppliers. If a quote changes, update the brief and re-run
   `source` (reject the stale approvals).
4. In the dashboard `/approvals`, approve **one** supplier, reject the rest.
5. The worker publishes it on its next tick (when Shopify env vars are set),
   or run `python ecosystem/shopify_pipeline.py publish-approved`.
   The product is created as a draft, then set ACTIVE and published to the
   Online Store. Re-runs are idempotent; only one product per brief is
   published even if several suppliers are approved. State lives in
   `data/shopify_state.json`.

## Pricing

`price = (landed_cost + $0.30) / (1 − margin − 2.9%)`, rounded up to `.99`.
Ad spend is not in the price; the report shows **max ad cost per order**
that still leaves a 20% net margin — the ceiling for future ad campaigns.

## Not built yet

- Automated trend → brief generation (discovery agent output → brief).
- Automated supplier search (needs a sourcing data provider; Alibaba has no
  public buyer search API).
- Ads (Meta / Google, created paused, launched on approval).
- Ongoing SEO / content and performance optimisation loops.
