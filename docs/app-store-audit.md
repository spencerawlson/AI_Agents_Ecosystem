# App-store listing audit

HeyCatch audits app-store listings. The ecosystem's version is
`core/app_store_audit.py` plus the program
`ecosystem/app_store_audit.py`, reading the public record — Apple's
iTunes Search API (`/lookup?id=...`), no key, no sign-in:

```bash
python ecosystem/app_store_audit.py --list
python ecosystem/app_store_audit.py audit --app kayayiti
python ecosystem/app_store_audit.py audit --app farelune
python ecosystem/app_store_audit.py audit --app-id 6826322327 --country ca
```

Reports: `reports/app_store_<app>_<date>.md`.

Configured apps (Apple IDs are public store identifiers, not
secrets): `kayayiti` (6826322327, storefront `ca`), `farelune`
(6828243842, storefront `us`).

## What it checks on a live listing

- Title ≤ 30 characters (Apple's limit).
- Description: < 200 characters is thin (high), < 500 is a medium
  finding.
- Screenshots: none is high, fewer than 3 is medium.
- Ratings: none yet is medium; an average under 4.0 is medium.
- Freshness: the current version older than 180 days, or empty
  "What's New" notes.
- Localization: a single language when the audience speaks several.

Score: 0–100, critical −25, high −15, medium −8, low −3.

## What it does NOT check

Keywords — Apple does not expose the keyword field publicly; that
lives in App Store Connect. Screenshot content and review text are
not scored either. The report names those gaps.

## The not-listed case

An app that is not on the store yet returns `resultCount: 0` from
the lookup. "Ready for Review" and "Prepare for Submission" both
look like this — Kay Ayiti and Farelune both returned 0 when the
module was built (November 7, 2026). The audit reports "not listed"
with the storefronts checked (the requested country, then the US
storefront as a second check, since listings can be
territory-limited) and a readiness checklist to verify in App Store
Connect — title ≤ 30, subtitle ≤ 30, keywords ≤ 100 characters,
description, 3+ screenshots, privacy policy URL, age rating —
instead of inventing a score for a listing nobody can see. When an
app goes live, re-run the same command and the real listing is
scored.
