# Analyze any site for a market

HeyCatch's onboarding is: paste a site link, get the analysis. The
ecosystem's version is `ecosystem/analyze.py` — no hardcoded business,
no state file, no signup. Type a URL, name the market, get one report:

```bash
python ecosystem/analyze.py analyze --url https://example.com --country ca
python ecosystem/analyze.py analyze --url https://example.com --country ca \
    --language fr --community r/quebec --keywords "déménagement, logement"
python ecosystem/analyze.py analyze --url https://example.com --country us \
    --app-id 6828243842
python ecosystem/analyze.py        # no arguments: interactive prompts
```

With no arguments the program prompts for each field (URL and
country are required; language, community, keywords and app ID are
optional — press Enter to skip). Reports land in
`reports/analyze_<host-slug>_<date>.md`.

## What each market input does

- `--url` — the site to analyze. Must start with `http://` or
  `https://`.
- `--country` — the market country code (`ca`, `us`, `ht`, ...). It
  sets the App Store storefront for the listing audit and is recorded
  on the report.
- `--language` — the market language (`en`, `fr`, `ht`, ...). The
  homepage's `<html lang>` is checked against it, and the report
  flags a missing or mismatched language and missing `hreflang`
  alternates.
- `--community` — a target subreddit (`r/cissp` or `cissp`). Recorded
  as the target audience. Listening only runs when `--keywords` is
  also given — keywords are the market's own words, never invented.
- `--keywords` — comma-separated keywords for community listening.
- `--app-id` — an App Store app ID. Runs the listing audit in the
  `--country` storefront (see `app-store-audit.md`).

## What runs

- **Site audit** (`core/site_audit.py`): crawl, sitemap, titles,
  descriptions, thin pages, the SPA-shell finding.
- **GEO audit** (`core/geo_audit.py`): AI-crawler access, llms.txt,
  answer-ready pages.
- **Market fit**: homepage language + hreflang, as above.
- **Community listening** (`core/community_listening.py`): Reddit
  search for the keywords in the community, scored 0–100, threads
  worth a reply (score ≥ 50) listed. Read-only: nothing is posted,
  no replies are drafted.
- **App-store audit** (`core/app_store_audit.py`): only with
  `--app-id`.

Scores are 0–100 from the findings (critical −25, high −15,
medium −8, low −3); the site score uses the same penalties as the
GEO score so the two are comparable.

## What it cannot do

- **No live social APIs.** X and Threads are listed as unavailable
  in every report — no public search without API credentials.
- **Reddit needs a residential IP.** From a datacenter IP Reddit
  blocks the search and the report says `unavailable` with the
  reason. Run listening from FedServ, like Google Trends.
- **No competitor data, no traffic data, no auto-publish.** The
  report is a draft for the owner to act on. Actual citations in
  ChatGPT / Perplexity / AI Overviews are not measured — spot-check
  them by hand.
- **A failed fetch is reported, not scored.** If the site cannot be
  reached at audit time, the report says so and asks for a re-run
  instead of blaming the site for a network problem.
