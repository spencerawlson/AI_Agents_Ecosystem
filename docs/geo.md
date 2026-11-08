# GEO — AI-search visibility

HeyCatch sells an "AI-search visibility" check: when someone asks
ChatGPT, Perplexity or Google's AI Overviews a question, does your
site get cited? The ecosystem's version is `core/geo_audit.py`, run
from the Road to CISSP program:

```bash
python businesses/road2cissp/growth.py geo
python businesses/road2cissp/growth.py status   # shows the last GEO score
```

Real HTTP, no key, no LLM, no spend. Report:
`reports/geo_<date>.md`. State: `state["geo"]` (score, finding
count, pages checked, llms.txt presence).

## What it measures

- **AI-crawler access.** `robots.txt` is parsed for the bots that
  feed AI answers: GPTBot, ChatGPT-User, OAI-SearchBot, CCBot,
  PerplexityBot, Google-Extended, ClaudeBot, anthropic-ai,
  cohere-ai, Meta-ExternalAgent, Applebot-Extended, DuckAssistBot.
  A blocked bot is critical — an engine cannot cite what it cannot
  crawl. A `Disallow: /` for everyone is critical. A robots file
  that is missing, or that returns the app shell instead of rules
  (a client-rendered SPA answering every path with HTML), is
  reported as "not a real robots file", not scored as fine.
- **llms.txt.** The emerging standard (llmstxt.org): a Markdown map
  of the site at `/llms.txt`. Absent is a high finding.
- **Answer-ready pages.** For every page in the sitemap (capped at
  10): exactly one H1; a direct-answer first paragraph of 40–300
  characters under the H1 (the shape AI answers quote); an FAQ
  section (`FAQPage` JSON-LD or an FAQ heading); JSON-LD structured
  data of any kind; at least one outbound citation link. Each is
  aggregated across pages, so one thin page does not sink the score
  and a site-wide gap shows as one finding.

Score: 0–100, critical −25, high −15, medium −8, low −3.

## What it does NOT measure

Actual citations in ChatGPT / Perplexity / AI Overviews. There is
no honest way to ask an AI engine "did you cite me" without an API
key or a manual check. The report says so on every run, and the
score is a **readiness** score, not a citation count. To check
citations, ask Perplexity about a topic the site covers and see if
road2cissp.com is cited — by hand, once a month.

## Why road2cissp.com scores low today

The site is a client-rendered SPA: every path (robots.txt,
sitemap.xml, llms.txt included) answers with the same app shell.
AI crawlers see one thin page, not a site — the same root cause the
technical site audit already flags (prerender the routes). Until
the routes are prerendered and real robots.txt / sitemap.xml /
llms.txt are served, the GEO score stays low no matter what copy
changes are made. Fix the rendering first; the GEO audit then
measures the content fixes.
