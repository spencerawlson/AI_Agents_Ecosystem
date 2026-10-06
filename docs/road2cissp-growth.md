# Road to CISSP growth program

The agents' main job is growing **road2cissp.com** (source: `SaaS & Portfolio/IT_Fund`).
They research, plan, write and measure on a loop. You approve what runs and
you post what they write.

```
audit + keywords + metrics ──► weekly strategy ──► experiments (you approve)
          ▲                                               │
          │                                               ▼
   outcomes feed next week ◄── duration ends ◄── content drafts (you post)
```

| Job | Agent | Cadence | Needs |
|-----|-------|---------|-------|
| Site audit: crawls every sitemap URL for titles, meta, canonical, og:image, crawlable text, analytics | marketing `site_audit` | 7 days | nothing |
| Keyword trends (Google Trends, 12 months) | marketing `seo_keyword_map` | 7 days | `pytrends` |
| Traffic snapshot (visitors, pageviews, top sources/pages) | Plausible API | daily | `PLAUSIBLE_API_KEY` (optional) |
| Strategy: north-star metric, diagnosis, ICE-ranked channel bets, 2–4 experiments, site fixes, this-week tasks | marketing `growth_strategy` (smart model) | 7 days | LLM key |
| Content drafts (blog, landing page, social, community answers, email) | creative `content_pieces` | once per approved experiment | LLM key |

Every experiment has a hypothesis, KPI, target, duration, budget (usually $0,
organic) and stop rule. Each one goes to `/approvals` as a **Marketing
experiment** card. When you approve it, the content agent drafts what it needs.
When its duration ends, it moves to `review` until you record how it went. That
outcome, plus measured traffic, shapes the next week's strategy. An experiment
you've rejected or finished is never proposed again under the same slug.

## What the agents do vs. what you do

| Action | Who |
|--------|-----|
| Crawl the site, research keywords, pull analytics | Agents |
| Write the strategy and propose experiments | Agents |
| Draft posts, articles, landing pages and community answers | Agents (files only) |
| Decide which experiments run | **You** (`/approvals`) |
| Publish anything, or spend any money | **You**. Nothing is posted or bought automatically. |
| Record experiment outcomes | **You** (`outcome` command) |

## Run it

The worker runs it automatically every tick (cadences live in the state file,
so frequent ticks are cheap):

```bash
export OPENAI_API_KEY=...          # or ANTHROPIC_API_KEY / GEMINI_API_KEY
export PLAUSIBLE_API_KEY=...       # optional, once analytics is installed
python launch.py worker            # growth program is the default focus
```

By hand:

```bash
python ecosystem/road2cissp_growth.py run                   # run whatever is due
python ecosystem/road2cissp_growth.py run --force all       # rerun every job now
python ecosystem/road2cissp_growth.py status                # experiments + last runs
python ecosystem/road2cissp_growth.py record-metrics --visitors 420 --signups 12
python ecosystem/road2cissp_growth.py outcome reddit-answers --worked "62 referral visits, 9 sign-ins"
```

Outputs:
- `reports/road2cissp_growth_<date>.md`: the weekly report, also shown on the
  dashboard at `/reports`
- `reports/road2cissp_drafts/<experiment>/`: drafts ready for you to post
- `data/road2cissp_growth.json`: program state (cadences, experiments, metrics)

Without an LLM key, the audit, keywords and metrics jobs still run. Strategy and
content are skipped, and the report lists the critical audit fixes as your to-do
list. A failed job retries after 6 hours, not every tick.

## Budgets

Hard caps per agent task: audit $0.50, keywords $1, strategy $3, content $2.
A normal week costs cents. All spend goes to the ledger under *Road to CISSP*.

## Current audit (first live run, 2026-10-06)

1. **Critical: every URL serves the same client-rendered shell.** All 15 sitemap
   pages return the same title and 0 words of text, so Google sees one page.
   Fix: prerender public routes and set per-route title/description/canonical.
2. **Critical: no analytics.** Nothing can be measured. Fix: install Plausible
   (or Umami/Vercel Analytics) and verify the domain in Search Console.
3. Medium: no canonical URLs. No og:image, so shared links have no preview.
4. Low: robots.txt doesn't reference the sitemap.

Fix 1 and 2 first. Until then, organic growth and measurement are both capped.

## Other projects

Opportunity discovery (`--discovery`) and the Shopify store (`--store`,
[shopify-store.md](shopify-store.md)) still work but are off by default.
