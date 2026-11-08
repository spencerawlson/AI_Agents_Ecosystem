"""App-store listing audit: is the listing earning its downloads?

HeyCatch audits app-store listings. The ecosystem's version reads the
public record — Apple's iTunes Search API (``/lookup?id=...``), no
key, no sign-in — and scores what a shopper actually sees. It is the
same shape as the site audit: findings with a severity and a fix,
and a 0–100 score (critical −25, high −15, medium −8, low −3).

What it checks on a live listing:

- Title ≤ 30 characters (Apple's limit), description meaningful
  (≥ 500 characters is healthy, < 200 is thin).
- Screenshots: fewer than 3 is thin, none is a blocker.
- Ratings: no ratings yet, or an average under 4.0.
- Freshness: the current version is older than 180 days.
- What's new: empty release notes.
- Languages: a single language when the audience speaks several.

What it does NOT check: keywords (Apple does not expose the keyword
field publicly — that lives in App Store Connect), screenshots'
content, or review text. The report names those gaps instead of
scoring them.

And the honest case first: an app that is not on the store yet
returns ``resultCount: 0`` from the lookup — "Ready for Review" and
"Prepare for Submission" both look like this. The audit reports
"not listed" with the storefronts checked (the requested country,
then the US storefront as a second check, since listings can be
territory-limited) and a readiness checklist to verify in App Store
Connect (title ≤ 30, subtitle ≤ 30, keywords ≤ 100 characters,
description, 3+ screenshots, privacy policy URL, age rating) —
instead of inventing a score for a listing nobody can see.

Reuses the repo's Fetcher pattern: every function takes a ``fetch``
returning ``(status, body)``, so tests inject fake iTunes JSON.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

from core.site_audit import Fetcher, http_fetch

PENALTIES = {"critical": 25, "high": 15, "medium": 8, "low": 3}
STALE_DAYS = 180

READINESS_CHECKLIST = (
    "title ≤ 30 characters, subtitle ≤ 30, keywords ≤ 100 characters, "
    "a description that says what the app does, 3+ screenshots, a "
    "privacy policy URL, and an age rating"
)


def lookup(app_id: str, country: str = "ca",
           fetch: Fetcher = http_fetch) -> dict | None:
    """Public iTunes lookup for one app. Tries the requested country,
    then the US storefront (listings can be territory-limited).
    Returns the app record, or None when it is not on the store.
    Raises ValueError on a network failure (not on "not listed")."""
    for storefront in (country, "us") if country != "us" else ("us",):
        url = ("https://itunes.apple.com/lookup?"
               + urllib.parse.urlencode({"id": app_id,
                                         "country": storefront}))
        status, body = fetch(url)
        if status == 0:
            raise ValueError(
                f"iTunes lookup failed (network failure, {storefront})")
        if status != 200:
            raise ValueError(f"iTunes lookup failed (HTTP {status})")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise ValueError("iTunes lookup returned bad JSON") from exc
        results = payload.get("results") or []
        if results:
            record = dict(results[0])
            record["_storefront"] = storefront
            return record
    return None


def _days_since(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - dt).days


def audit_app(app_id: str, country: str = "ca",
              fetch: Fetcher = http_fetch,
              now: datetime | None = None) -> dict:
    """Audit one app by its Apple ID. Returns the score (None when not
    listed — there is no listing to score), the findings, and the
    listing facts the checks used."""
    now = now or datetime.now(timezone.utc)
    record = lookup(app_id, country=country, fetch=fetch)
    if record is None:
        return {
            "app_id": str(app_id),
            "listed": False,
            "score": None,
            "storefronts_checked": [country] + (
                ["us"] if country != "us" else []),
            "findings": [{
                "severity": "medium", "area": "listing",
                "issue": "not on the App Store in the storefronts checked",
                "fix": ("Check the status in App Store Connect. When it is "
                        "live, re-run this audit. Before submitting, verify "
                        f"the listing meets Apple's limits: {READINESS_CHECKLIST}."),
            }],
            "details": {},
            "note": "No public listing exists to score — this is a "
                    "readiness check, not a listing score.",
        }
    findings: list[dict] = []

    def find(severity, area, issue, fix):
        findings.append({"severity": severity, "area": area,
                         "issue": issue, "fix": fix})

    name = str(record.get("trackName", ""))
    description = str(record.get("description", "") or "")
    screenshots = list(record.get("screenshotUrls") or [])
    ratings = int(record.get("userRatingCount") or 0)
    average = record.get("averageUserRating")
    version = str(record.get("version", ""))
    release_notes = str(record.get("releaseNotes", "") or "")
    languages = list(record.get("languageCodesISO2A") or [])
    age_days = _days_since(record.get("currentVersionReleaseDate"), now)

    if len(name) > 30:
        find("high", "title",
             f"title is {len(name)} characters (Apple's limit is 30)",
             "Shorten the app name to 30 characters or fewer — the store "
             "truncates the rest.")
    if len(description) < 200:
        find("high", "description",
             f"description is only {len(description)} characters",
             "Write a description that says what the app does, who it is "
             "for, and why it is better — aim for 500+ characters.")
    elif len(description) < 500:
        find("medium", "description",
             f"description is {len(description)} characters (thin)",
             "Expand the description toward 500+ characters with the "
             "app's key features and who it serves.")
    if not screenshots:
        find("high", "screenshots", "no screenshots on the listing",
             "Add screenshots — a listing with none converts poorly.")
    elif len(screenshots) < 3:
        find("medium", "screenshots",
             f"only {len(screenshots)} screenshot(s) on the listing",
             "Add screenshots showing the core flows (3+).")
    if ratings == 0:
        find("medium", "ratings", "no ratings yet",
             "Ask early users to rate the app — zero ratings reads as "
             "untried.")
    elif average is not None and float(average) < 4.0:
        find("medium", "ratings",
             f"average rating is {average:.1f} from {ratings} ratings",
             "Read the low reviews and fix the top complaint before "
             "spending on promotion.")
    if age_days is not None and age_days > STALE_DAYS:
        find("low", "freshness",
             f"current version {version or '?'} is {age_days} days old",
             "Ship an update — a stale version signals an unmaintained app.")
    if not release_notes.strip():
        find("low", "freshness", "no 'What's New' notes on this version",
             "Write release notes for each version — they are the "
             "changelog shoppers read.")
    if len(languages) == 1:
        find("low", "localization",
             f"listing is in one language ({languages[0]})",
             "Localize the listing for the audience's languages — the "
             "app's users do not all read one language.")
    score = max(0, 100 - sum(PENALTIES.get(f["severity"], 0)
                             for f in findings))
    return {
        "app_id": str(app_id),
        "listed": True,
        "score": score,
        "storefront": record.get("_storefront"),
        "findings": findings,
        "details": {
            "name": name,
            "version": version,
            "description_chars": len(description),
            "screenshots": len(screenshots),
            "ratings": ratings,
            "average_rating": average,
            "languages": languages,
            "current_version_age_days": age_days,
        },
        "note": "Keywords are not visible publicly — verify the "
                "100-character keyword field in App Store Connect.",
    }
