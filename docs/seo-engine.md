# SEO Engine

A GrandRanker-class SEO content autopilot built on the ecosystem's own
agents — run on Spencer's own sites first, productized later.

## Pipeline

1. **Inventory** (`core/seo_content.fetch_inventory`) — crawls the target
   site's real sitemap over HTTP. These pages are the only legal targets
   for internal links.
2. **Research** (`marketing/seo_keyword_map`) — seed keywords scored on
   live Google Trends data. Gaps are marked `unavailable`; numbers are
   never invented.
3. **Writing** (`creative/seo_article`) — the LLM writes a full article
   package (title, meta description, slug, Markdown body, FAQ, internal
   links) constrained to the site facts and candidate links it is given.
4. **Quality gate** (`core/seo_content.validate_article_package`) — the
   deterministic quintuple-check layer for this pipeline. Hard errors
   block publishing: hallucinated internal links, slug collisions with
   the live site, keyword stuffing, thin copy, missing FAQ, placeholder
   text. The creative agent's `self_check` surfaces gate errors to the
   orchestrator, so a failing draft becomes rework feedback, not output.
5. **Publishing** (`core/seo_publish`) — explicit and draft-first:
   - `--publish export`: draft article files (front matter + JSON-LD +
     Markdown) for a Next.js / static site repo.
   - `--publish wordpress`: posts created via the WordPress REST API
     with status `draft`.
   Nothing goes live from this mission, ever.

## Run it

```bash
export OPENAI_API_KEY=...            # same requirement as every mission
python3 ecosystem/seo_engine_mission.py \
    --site-url https://farelune.com \
    --niche "flight comparison and travel planning" \
    --seeds "cheap flights,flights to paris,how to find cheap flights" \
    --fact "Farelune compares flights and shows flight times from the visitor's own city" \
    --count 3 --publish export
```

The report lands in `reports/seo_engine_<domain>_<date>.md` and an
approval request is queued, exactly like the marketing mission.

WordPress mode reads `WP_USER` / `WP_APP_PASSWORD` from the environment
(`WP_SITE_URL` overrides the target site). Create the application
password in WP Admin → Users → Profile → Application Passwords.

## Notes and limits

- Google Trends rate-limits datacenter IPs (HTTP 429). Run the mission
  from a residential connection (the Fedora server) for live keyword
  data; from a blocked IP the keyword map degrades to `unavailable`
  markers instead of failing.
- The WordPress Markdown→HTML converter covers the writer's controlled
  subset (headings, paragraphs, bold/italic, links, bullet lists).
- Ranking / AI-visibility tracking (the Growth agent's slice of the
  GrandRanker feature set) is not in v1 — v1 is research → write →
  gate → draft-publish.
