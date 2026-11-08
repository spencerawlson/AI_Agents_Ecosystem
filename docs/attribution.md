# Funnel attribution

HeyCatch's funnel analytics trace every visit and conversion to the
channel that earned it. The ecosystem's version connects the pieces
it already had — Plausible traffic snapshots, owner-recorded Stripe
revenue — instead of inventing a tracking pixel it does not run.

```bash
python ecosystem/road2cissp_growth.py record-conversion --amount 19 --touches "community:r/cissp"
python ecosystem/road2cissp_growth.py record-revenue --amount 190 --channel community --source r/cissp
python ecosystem/road2cissp_growth.py attribution            # last-touch, Plausible 30d when the key is set
python ecosystem/road2cissp_growth.py attribution --model linear
python ecosystem/road2cissp_growth.py attribution --no-plausible
```

Or read it from the dashboard: `GET /api/attribution` (also linked
from the portfolio page).

## Channels

`seo`, `community`, `social`, `paid`, `referral`, `direct`, `email`,
`unknown`. UTM medium wins when present (`cpc` → paid, `organic` →
seo, `social` → social, `email` → email); otherwise the source name
decides (google/bing → seo, reddit → community, x/threads/linkedin
→ social, nothing → direct, anything else → referral).

## Conversions are owner-recorded

Road to CISSP has no signup webhook and no tracking pixel, so there
is no automatic conversion feed to read. A conversion is recorded by
hand with its touch journey — every channel that brought this learner
before they paid, in order:

```bash
record-conversion --amount 19 --touches "seo:google, community:r/cissp"
```

A conversion with one touch is first-touch and last-touch at once;
the report says so. Nothing here invents a journey. `record-revenue
--channel` / `--touches` is the same thing in one step: it records
the conversion *and* books the revenue (once — never double-booked).

## Models

- **last_touch** (default) — full credit to the last touch.
- **first_touch** — full credit to the first touch.
- **linear** — credit split evenly across the journey.

Same conversions, three views; the report prints the model it used.

## Sessions

Sessions per channel come from Plausible's `visit:source` breakdown
(30d) when `PLAUSIBLE_API_KEY` is set, or from the latest recorded
snapshot's `top_sources` when it is not. When neither exists,
sessions are **unavailable** — the report still attributes
conversions and revenue per channel, it just cannot compute a
conversion rate, and it says that plainly instead of dividing by an
invented number.

## Suggestions

Deterministic rules, not an LLM, at most three: a channel with
sessions but no conversions (the funnel leaks there), the best
converter with real evidence (double down), conversions with
unmeasured traffic (connect Plausible), revenue concentrated in one
channel (diversify). They feed the next strategy cycle; they do not
change the plan by themselves.

## Where it lives

- `core/attribution.py` — pure classification, models, aggregation,
  suggestions. No network, no LLM.
- `core/site_metrics.py` — `PlausibleClient.source_breakdown()`.
- `ecosystem/road2cissp_growth.py` — `record_conversion`,
  `run_attribution`, the `record-conversion` / `attribution` CLI.
- `dashboard/app.py` — `GET /api/attribution`.
- Reports at `reports/attribution_<yyyymmdd>.md`; the latest totals
  are kept in the growth state under `attribution_last`.
