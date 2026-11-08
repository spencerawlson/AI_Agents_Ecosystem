"""Funnel attribution: which channel brought the customer.

HeyCatch sells "funnel analytics" — every visit and conversion traced
to the channel that earned it. The ecosystem already had the pieces
in three places and none of them connected: an in-memory event funnel
(``core/analytics.py``), Plausible traffic snapshots with a
top-sources breakdown (``core/site_metrics.py``), and owner-recorded
Stripe revenue (the growth program's ``record-revenue``). This module
is the joint.

How it works, and what it honestly is:

- **Channels** are a fixed set: ``seo``, ``community``, ``social``,
  ``paid``, ``referral``, ``direct``, ``email``, ``unknown``.
  ``classify_source`` maps a referrer / UTM pair to one of them —
  UTM medium wins when present (cpc → paid, organic → seo, social →
  social, email → email), otherwise the source name decides
  (google/bing → seo, reddit → community, x/threads/linkedin →
  social, nothing → direct, anything else → referral).
- **Conversions are owner-recorded.** Road to CISSP has no signup
  webhook and no tracking pixel, so there is no automatic conversion
  feed to read — a conversion is recorded by hand
  (``record-conversion --amount 19 --touches "community:r/cissp"``),
  with its touch journey: every channel that brought this customer
  before they paid, in order. Nothing here invents a journey; a
  conversion with one touch is first-touch and last-touch at once,
  and the report says so.
- **Three models, all deterministic:** ``first_touch`` (full credit
  to the first touch), ``last_touch`` (full credit to the last —
  the default), ``linear`` (credit split evenly across touches).
  Same conversions, three views; the report prints the model it used.
- **Sessions come from Plausible** (``visit:source`` breakdown, 30d)
  when the key is set, or from the latest recorded snapshot's
  ``top_sources`` when it is not. When neither exists, sessions are
  ``unavailable`` — the report still attributes conversions and
  revenue per channel, it just cannot compute a conversion rate, and
  it says that plainly instead of dividing by an invented number.
- **Suggestions are rules, not an LLM.** A channel with sessions
  but no conversions, the best converter, revenue concentrated in
  one channel, conversions with unmeasured traffic — each produces
  one plain-language next move. They feed the next strategy cycle;
  they do not change the plan by themselves.

Pure functions throughout: no network, no LLM, no spend. The growth
program wires the live pieces (Plausible fetch, state, report) around
them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

CHANNELS = ("seo", "community", "social", "paid", "referral",
            "direct", "email", "unknown")

MODELS = ("first_touch", "last_touch", "linear")

_MEDIUM_CHANNELS = {
    "cpc": "paid", "ppc": "paid", "paid": "paid", "cpm": "paid",
    "display": "paid", "organic": "seo", "social": "social",
    "social-media": "social", "email": "email", "referral": "referral",
    "community": "community",
}

_SOURCE_CHANNELS = {
    "google": "seo", "bing": "seo", "duckduckgo": "seo", "yahoo": "seo",
    "ecosia": "seo", "brave": "seo", "reddit": "community",
    "hacker news": "community", "news.ycombinator.com": "community",
    "twitter": "social", "x.com": "social", "threads": "social",
    "linkedin": "social", "facebook": "social", "instagram": "social",
    "youtube": "social", "tiktok": "social", "newsletter": "email",
    "direct": "direct",
}


def parse_utm(url: str) -> dict:
    """UTM parameters from a landing URL ({} when there are none)."""
    try:
        qs = parse_qs(urlparse(url or "").query)
    except Exception:  # noqa: BLE001 - a malformed URL is just no UTMs
        return {}
    out = {}
    for key in ("utm_source", "utm_medium", "utm_campaign"):
        values = qs.get(key)
        if values:
            out[key[4:]] = values[0]
    return out


def classify_source(source: str | None, medium: str | None = None,
                    landing_url: str | None = None) -> str:
    """One referrer / UTM pair -> one channel. UTM medium is the
    reliable signal when present; the source name decides otherwise."""
    utm = parse_utm(landing_url or "")
    medium = (medium or utm.get("medium") or "").strip().lower()
    if medium:
        if medium in _MEDIUM_CHANNELS:
            return _MEDIUM_CHANNELS[medium]
        if medium in ("none", "(none)"):
            return "direct"
    source = (source or utm.get("source") or "").strip().lower()
    if not source or source in ("direct", "(direct)", "road2cissp.com"):
        return "direct"
    if source.startswith("r/"):
        return "community"  # a subreddit name, e.g. r/cissp
    for name, channel in _SOURCE_CHANNELS.items():
        if name in source:
            return channel
    return "referral"


def classify_touch(touch: dict) -> str:
    """A touch is {"source", "medium", "landing_url"} — or carries an
    explicit "channel" the owner tagged, which wins."""
    channel = (touch.get("channel") or "").strip().lower()
    if channel in CHANNELS:
        return channel
    return classify_source(touch.get("source"), touch.get("medium"),
                           touch.get("landing_url"))


def parse_touches(spec: str) -> list[dict]:
    """Parse the CLI touch format: comma-separated "channel" or
    "channel:source", in journey order. "community:r/cissp, seo:google"
    -> [{"channel": "community", "source": "r/cissp"}, ...].
    Raises ValueError on an unknown channel."""
    touches = []
    for raw in (spec or "").split(","):
        part = raw.strip()
        if not part:
            continue
        if ":" in part:
            channel, source = part.split(":", 1)
            touch = {"channel": channel.strip().lower(),
                     "source": source.strip()}
        else:
            touch = {"channel": part.lower()}
        if touch["channel"] not in CHANNELS:
            raise ValueError(
                f"unknown channel {touch['channel']!r} "
                f"(one of: {', '.join(CHANNELS)})")
        touches.append(touch)
    if not touches:
        raise ValueError("at least one touch is required "
                         "(e.g. --touches \"community:r/cissp\")")
    return touches


def _credit(conversion: dict, model: str) -> dict[str, float]:
    """How much of one conversion each channel gets, per model.
    Returns {channel: fraction} summing to 1.0."""
    channels = [classify_touch(t) for t in conversion.get("touches", [])]
    if not channels:
        return {"unknown": 1.0}
    if model == "first_touch":
        return {channels[0]: 1.0}
    if model == "linear":
        share = 1.0 / len(channels)
        out: dict[str, float] = {}
        for channel in channels:
            out[channel] = out.get(channel, 0.0) + share
        return out
    return {channels[-1]: 1.0}  # last_touch (default)


def aggregate(conversions: list[dict],
              sessions_by_channel: dict[str, int] | None,
              model: str = "last_touch") -> dict:
    """Per-channel attribution for a list of conversions.

    Returns {"channels": [...], "totals": {...}} where each channel row
    carries sessions (None when unmeasured), conversions (float —
    fractional under linear), revenue, conversion rate (None when
    sessions are unknown or zero) and revenue per session."""
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r} (one of: {', '.join(MODELS)})")
    rows: dict[str, dict] = {}
    total_conversions = 0.0
    total_revenue = 0.0
    for conv in conversions:
        amount = float(conv.get("amount_usd") or 0)
        for channel, fraction in _credit(conv, model).items():
            row = rows.setdefault(channel, {"channel": channel,
                                            "conversions": 0.0,
                                            "revenue": 0.0})
            row["conversions"] += fraction
            row["revenue"] += amount * fraction
        total_conversions += 1.0
        total_revenue += amount
    for channel, sessions in (sessions_by_channel or {}).items():
        rows.setdefault(channel, {"channel": channel, "conversions": 0.0,
                                  "revenue": 0.0})
    channels = []
    for channel in sorted(rows):
        row = rows[channel]
        sessions = (sessions_by_channel or {}).get(channel)
        conv = row["conversions"]
        channels.append({
            "channel": channel,
            "sessions": sessions,
            "conversions": round(conv, 2),
            "revenue": round(row["revenue"], 2),
            "conversion_rate": (round(conv / sessions, 4)
                                if sessions else None),
            "revenue_per_session": (round(row["revenue"] / sessions, 4)
                                    if sessions else None),
        })
    total_sessions = (sum((sessions_by_channel or {}).values())
                      if sessions_by_channel else None)
    return {
        "channels": channels,
        "totals": {
            "sessions": total_sessions,
            "conversions": round(total_conversions, 2),
            "revenue": round(total_revenue, 2),
            "conversion_rate": (round(total_conversions / total_sessions, 4)
                                if total_sessions else None),
        },
    }


def suggest(attribution: dict) -> list[dict]:
    """Deterministic next moves from an aggregation. At most three,
    ordered by how much they matter. Each carries the signal it was
    derived from, so the owner can check it."""
    rows = attribution["channels"]
    totals = attribution["totals"]
    suggestions: list[dict] = []
    if totals["conversions"] == 0:
        suggestions.append({
            "channel": None,
            "signal": "no conversions recorded yet",
            "suggestion": "Record a conversion with `record-conversion` "
                          "the next time a learner pays, with the channel "
                          "that brought them — attribution has nothing to "
                          "learn from until then."})
        return suggestions
    # Sessions with no conversions: the funnel leaks here.
    for row in rows:
        if (row["sessions"] or 0) >= 50 and row["conversions"] == 0:
            suggestions.append({
                "channel": row["channel"],
                "signal": f"{row['sessions']} sessions, 0 conversions",
                "suggestion": f"{row['channel']} brings traffic that never "
                              "pays — check the landing page and the offer "
                              "before spending more effort there."})
    # Best converter with real evidence: double down.
    measured = [r for r in rows
                if r["conversion_rate"] is not None and r["conversions"] >= 2]
    if measured:
        best = max(measured, key=lambda r: r["conversion_rate"])
        suggestions.append({
            "channel": best["channel"],
            "signal": (f"{best['conversion_rate']:.2%} conversion rate "
                       f"({best['conversions']} conversions)"),
            "suggestion": f"{best['channel']} converts best — put the next "
                          "content and community effort there first."})
    # Converting with unmeasured traffic: fix measurement, not the channel.
    for row in rows:
        if row["conversions"] > 0 and row["sessions"] is None:
            suggestions.append({
                "channel": row["channel"],
                "signal": f"{row['conversions']} conversions, sessions unknown",
                "suggestion": f"{row['channel']} converts but its traffic "
                              "is unmeasured — connect Plausible source "
                              "breakdown so the rate is real."})
    # Revenue concentrated in one channel: a single point of failure.
    if totals["revenue"] > 0:
        top = max(rows, key=lambda r: r["revenue"])
        if top["revenue"] / totals["revenue"] > 0.8 and len(rows) > 1:
            suggestions.append({
                "channel": top["channel"],
                "signal": (f"{top['revenue'] / totals['revenue']:.0%} of "
                           "revenue from one channel"),
                "suggestion": f"Revenue depends on {top['channel']} alone "
                              "— build a second channel before it dips."})
    return suggestions[:3]


def sessions_from_snapshot(snapshot: dict | None) -> dict[str, int] | None:
    """Map a Plausible snapshot's top_sources to sessions per channel.
    Returns None when the snapshot has no usable sources."""
    if not snapshot:
        return None
    out: dict[str, int] = {}
    for entry in snapshot.get("top_sources") or []:
        name = entry.get("name")
        visitors = entry.get("visitors")
        if not name or visitors is None:
            continue
        channel = classify_source(str(name))
        out[channel] = out.get(channel, 0) + int(visitors)
    return out or None


def build_attribution(state: dict,
                      sessions_by_channel: dict[str, int] | None = None,
                      sessions_source: str = "unavailable",
                      model: str = "last_touch",
                      now: datetime | None = None) -> dict:
    """The full attribution view for the dashboard, the CLI and the
    report: aggregation + suggestions + provenance. When no fresh
    sessions are passed, the latest recorded metrics snapshot is
    used; when that has no sources either, sessions stay unavailable
    and the report says so."""
    now = now or datetime.now(timezone.utc)
    if sessions_by_channel is None:
        metrics = (state.get("metrics") or [])
        sessions_by_channel = sessions_from_snapshot(
            metrics[-1] if metrics else None)
        if sessions_by_channel is not None:
            sessions_source = "recorded snapshot"
    conversions = list(state.get("conversions") or [])
    result = aggregate(conversions, sessions_by_channel, model=model)
    result.update({
        "generated_at": now.isoformat(),
        "model": model,
        "conversions_count": len(conversions),
        "sessions_source": sessions_source,
        "suggestions": suggest(result),
    })
    return result
