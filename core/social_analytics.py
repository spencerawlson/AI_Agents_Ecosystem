"""Social media analytics + reporting for marketing departments.

HeyCatch sells social listening and analytics. The ecosystem has
community listening (draft replies from Reddit threads) but no
analytics: nobody could answer "which channel earns the most
engagement, when should we post, are we growing?" This module is
that answer — from the owner's own platform exports, not from a
live API, because there is no live API to call: X and Threads are
login-walled with no credentials in the repo, and the other
platforms need the owner's own account tokens. Live platform data
comes from the owner's own exports (Instagram Insights / Meta
Business Suite, X Analytics, TikTok, YouTube Studio). Reddit live
data is a follow-up, not this build.

## Input formats

CSV — one row per post, plus optional follower-snapshot rows.
Column names are case-insensitive; aliases are accepted:

- ``date`` (YYYY-MM-DD, also YYYY/MM/DD or an ISO datetime),
  ``time`` (HH:MM) or ``posted_at`` (ISO datetime) for posting time
- ``channel`` — instagram, x, tiktok, youtube, facebook, linkedin,
  threads (anything else is kept as-is, lowercased)
- ``post_id`` (optional), ``caption`` / ``text`` / ``title``
  (optional, used for keyword sentiment and top-post labels),
  ``post_type`` (optional: reel, photo, video, text, link, ...)
- ``likes``, ``comments``, ``shares``, ``saves`` (counts;
  ``comments`` is always a count, never text), ``reach``,
  ``impressions``, ``views``
- ``followers`` — follower count on that date
- ``row_type`` (optional: ``post`` or ``snapshot``). Without it a
  row with a follower count and no engagement columns is a
  snapshot; everything else is a post.

Numbers may be plain (1234), comma-grouped ("1,234") or suffixed
("1.2k" = 1200, "2.5m"). A row that cannot be parsed (bad date,
no channel, no numbers at all) is skipped and counted, never
silently treated as zero.

JSON — either a list of objects with the same field names, or an
object with ``"posts"`` and/or ``"snapshots"`` lists.

## What it computes

Per channel and overall: posts, likes, comments, shares, saves,
interactions, reach, impressions, latest follower count,
engagement rate, top posts by engagement, best posting hour and
weekday by average interactions, follower growth when 2+ dated
follower observations exist, and keyword sentiment.

- **Engagement rate** is interactions / reach when reach is
  present, else interactions / followers, else unavailable. The
  report always says which denominator was used — a rate without
  its denominator is a lie.
- **Best posting time** needs at least 2 posts in an hour/weekday
  bucket to win, so one viral post cannot crown an hour. If no
  time data exists, the field is omitted and the report says so.
- **Follower growth** is last minus first observation, with a
  percentage and a per-day rate. One observation is not growth;
  it is reported as unavailable.
- **Keyword sentiment is not AI.** A fixed positive/negative word
  list is counted in captions and titles (word boundaries,
  case-insensitive). The report labels it "keyword sentiment, not
  AI" every time, and omits it entirely when there is no text.
- **Recommendations are deterministic rules**, max 5, plain
  language: the weakest channel, the best posting time, the best
  post type, flat or falling followers, missing reach data,
  negative-skewing sentiment.

## Honesty rules (repo-wide, enforced here)

- Missing metrics are ``"unavailable"`` — never silently zero.
  A channel with no reach has no reach-based rate; it gets the
  follower-based rate, and the denominator is named.
- Optional results are **omitted when empty**: no ``best_time``
  key when there is no time data, no ``follower_growth`` when
  there is one observation, no ``sentiment`` when there is no
  text. A report that prints an empty section is a report that
  looks finished when it is not.
- An export with zero usable rows raises ``ValueError`` — an
  action returning zero usable items raises instead of returning
  empty placeholders.
- **All charts are hand-built SVG strings** (no matplotlib, no
  new dependency, no raster images anywhere in this feature):
  engagement-by-channel bars, best-posting-time bars, follower
  growth lines. Pure functions, deterministic output.

No network, no LLM, no spend.
"""

from __future__ import annotations

import csv
import html
import io
import json
import math
import re
from datetime import date, datetime
from xml.sax.saxutils import escape as _xml_escape

# -- input normalisation ------------------------------------------------------

ALIASES = {
    "post id": "post_id", "id": "post_id",
    "caption": "caption", "text": "caption", "title": "caption",
    "post type": "post_type", "type": "post_type",
    "post_type": "post_type",
    "posted at": "posted_at", "posted_at": "posted_at",
    "published": "date", "publish date": "date",
    "followers": "followers", "follower count": "followers",
    "row type": "row_type", "row_type": "row_type",
}
# likes/comments/shares/saves/reach/impressions/views/channel/date/time
# already spell the canonical name, so they need no alias.

_NUMBER_RE = re.compile(r"^\s*([0-9][0-9,]*(\.[0-9]+)?)\s*([kKmM]?)\s*$")
_WORD_RE = re.compile(r"[a-z]+")

POSITIVE_WORDS = frozenset("""
    love great amazing awesome excellent beautiful perfect thanks thank
    happy wow best fantastic brilliant nice good cool helpful useful
    inspiring gorgeous wonderful lovely superb outstanding fun exciting
    win winning success happy grateful love
""".split())

NEGATIVE_WORDS = frozenset("""
    bad terrible awful hate worst boring useless broken slow
    disappointing poor ugly wrong annoying frustrating scam fake sad
    angry horrible disgusting waste fail failed problem issue complaint
""".split())

CHANNEL_COLORS = ["#6D28D9", "#0E7490", "#B45309", "#15803D", "#BE185D",
                  "#4D7C0F", "#0F766E", "#9D174D"]

RECOMMENDATION_LIMIT = 5


def _to_number(raw) -> int | float | None:
    """'1,234' / '1.2k' / 1234 -> a number. Anything else -> None."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return raw if math.isfinite(raw) else None
    text = str(raw).strip()
    if not text:
        return None
    m = _NUMBER_RE.match(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    suffix = m.group(3).lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value) if value.is_integer() else value


def _to_date(raw) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _to_time(raw) -> tuple[int, int] | None:
    """HH:MM (or an ISO datetime) -> (hour, minute)."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.hour, raw.minute
    text = str(raw).strip()
    if not text:
        return None
    if "T" in text:
        try:
            dt = datetime.fromisoformat(text)
            return dt.hour, dt.minute
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    return (hour, minute) if 0 <= hour < 24 and 0 <= minute < 60 else None


def _normalise_row(raw: dict) -> dict | None:
    """One raw row (CSV or JSON) -> a clean post/snapshot dict, or
    None when the row is unusable (no channel, no date, no data)."""
    row: dict = {}
    for key, value in raw.items():
        if key is None:
            continue
        name = ALIASES.get(str(key).strip().lower(),
                           str(key).strip().lower())
        row[name] = value
    channel = str(row.get("channel") or "").strip().lower()
    when = _to_date(row.get("date") or row.get("posted_at"))
    if not channel or when is None:
        return None
    out: dict = {"channel": channel, "date": when}
    for field in ("likes", "comments", "shares", "saves", "reach",
                  "impressions", "views", "followers"):
        value = _to_number(row.get(field))
        if value is not None:
            out[field] = value
    caption = str(row.get("caption") or "").strip()
    if caption:
        out["caption"] = caption
    post_type = str(row.get("post_type") or "").strip().lower()
    if post_type:
        out["post_type"] = post_type
    post_id = str(row.get("post_id") or "").strip()
    if post_id:
        out["post_id"] = post_id
    posted = _to_time(row.get("posted_at") or row.get("time"))
    if posted is not None:
        out["hour"] = posted[0]
        out["minute"] = posted[1]
    row_type = str(row.get("row_type") or "").strip().lower()
    engagement = any(f in out for f in
                     ("likes", "comments", "shares", "saves", "reach",
                      "impressions", "views"))
    if row_type == "snapshot":
        out["kind"] = "snapshot"
    elif row_type == "post":
        out["kind"] = "post"
    elif "followers" in out and not engagement:
        out["kind"] = "snapshot"
    else:
        out["kind"] = "post"
    if out["kind"] == "post" and not engagement and "caption" not in out:
        return None  # a post with no numbers and no text says nothing
    return out


def parse_csv(text: str) -> tuple[list[dict], int]:
    """CSV text -> (usable rows, skipped-row count)."""
    rows: list[dict] = []
    skipped = 0
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("no usable rows in this export — check the "
                         "column names against docs/social-analytics.md")
    for raw in reader:
        if raw is None or all(v in (None, "") for v in raw.values()):
            continue
        parsed = _normalise_row(raw)
        if parsed is None:
            skipped += 1
        else:
            rows.append(parsed)
    return rows, skipped


def parse_json(text: str) -> tuple[list[dict], int]:
    """JSON text -> (usable rows, skipped-row count). Accepts a list
    of rows, or {"posts": [...], "snapshots": [...]}."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if isinstance(payload, dict):
        raw_rows = list(payload.get("posts") or [])
        for snap in payload.get("snapshots") or []:
            if isinstance(snap, dict):
                raw_rows.append(dict(snap, row_type="snapshot"))
    elif isinstance(payload, list):
        raw_rows = payload
    else:
        raise ValueError("JSON export must be a list of rows or an "
                         "object with 'posts' / 'snapshots'")
    rows: list[dict] = []
    skipped = 0
    for raw in raw_rows:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        parsed = _normalise_row(raw)
        if parsed is None:
            skipped += 1
        else:
            rows.append(parsed)
    return rows, skipped


def parse_export(text: str, filename: str = "") -> tuple[list[dict], int]:
    """Parse an export by extension (.csv / .json). Raises ValueError
    when the file has zero usable rows or an unknown extension."""
    if filename.lower().endswith(".json"):
        rows, skipped = parse_json(text)
    elif filename.lower().endswith(".csv"):
        rows, skipped = parse_csv(text)
    else:
        raise ValueError("unknown export format: use a .csv or .json file")
    if not rows:
        raise ValueError("no usable rows in this export — check the "
                         "column names against docs/social-analytics.md")
    return rows, skipped


# -- metrics ------------------------------------------------------------------

def _interactions(post: dict) -> float:
    return sum(float(post.get(f) or 0)
               for f in ("likes", "comments", "shares", "saves"))


def _channel_stats(posts: list[dict], snapshots: list[dict]) -> dict:
    stats: dict = {
        "posts": len(posts),
        "likes": sum(int(p.get("likes") or 0) for p in posts),
        "comments": sum(int(p.get("comments") or 0) for p in posts),
        "shares": sum(int(p.get("shares") or 0) for p in posts),
        "saves": sum(int(p.get("saves") or 0) for p in posts),
        "reach": sum(int(p.get("reach") or 0) for p in posts),
        "impressions": sum(int(p.get("impressions") or 0) for p in posts),
    }
    stats["interactions"] = (stats["likes"] + stats["comments"]
                             + stats["shares"] + stats["saves"])
    # Latest follower count: newest dated observation, post or snapshot.
    observations = [(p["date"], p["followers"]) for p in posts + snapshots
                    if "followers" in p]
    if observations:
        stats["followers"] = max(observations, key=lambda o: o[0])[1]
    stats["avg_interactions_per_post"] = (
        round(stats["interactions"] / len(posts), 1) if posts else None)
    reach = stats["reach"]
    if reach > 0:
        stats["engagement_rate"] = round(stats["interactions"] / reach, 4)
        stats["engagement_rate_denominator"] = "reach"
    elif "followers" in stats and stats["followers"] > 0:
        stats["engagement_rate"] = round(
            stats["interactions"] / stats["followers"], 4)
        stats["engagement_rate_denominator"] = "followers"
    else:
        stats["engagement_rate"] = None
        stats["engagement_rate_denominator"] = "unavailable"
    if posts:
        top = max(posts, key=_interactions)
        stats["top_post"] = {
            "date": top["date"].isoformat(),
            "interactions": int(_interactions(top)),
            "caption": (top.get("caption") or "")[:120],
        }
        if "post_id" in top:
            stats["top_post"]["post_id"] = top["post_id"]
    return stats


def _best_time(posts: list[dict]) -> dict | None:
    """Best hour and weekday by average interactions. A bucket needs
    at least 2 posts to win, so one viral post cannot crown an hour.
    Returns None when no time data or no bucket qualifies."""
    timed = [p for p in posts if "hour" in p]
    if not timed:
        return None

    def best(buckets: dict) -> dict | None:
        qualified = {k: v for k, v in buckets.items() if len(v) >= 2}
        if not qualified:
            return None
        key = max(qualified, key=lambda k: sum(_interactions(p)
                                               for p in qualified[k])
                  / len(qualified[k]))
        return {"key": key,
                "avg_interactions": round(
                    sum(_interactions(p) for p in qualified[key])
                    / len(qualified[key]), 1),
                "posts": len(qualified[key])}

    hours: dict[int, list[dict]] = {}
    for p in timed:
        hours.setdefault(p["hour"], []).append(p)
    hour = best(hours)
    days: dict[str, list[dict]] = {}
    for p in timed:
        days.setdefault(p["date"].strftime("%A"), []).append(p)
    day = best(days)
    if hour is None and day is None:
        return None
    out: dict = {}
    if hour is not None:
        out["hour"] = hour["key"]
        out["hour_avg_interactions"] = hour["avg_interactions"]
        out["hour_posts"] = hour["posts"]
    if day is not None:
        out["weekday"] = day["key"]
        out["weekday_avg_interactions"] = day["avg_interactions"]
        out["weekday_posts"] = day["posts"]
    return out


def _hourly_averages(posts: list[dict]) -> list[dict]:
    """Average interactions per posting hour, for the chart."""
    buckets: dict[int, list[dict]] = {}
    for p in posts:
        if "hour" in p:
            buckets.setdefault(p["hour"], []).append(p)
    return [{"hour": h,
             "avg_interactions": round(
                 sum(_interactions(p) for p in ps) / len(ps), 1),
             "posts": len(ps)}
            for h, ps in sorted(buckets.items())]


def _follower_growth(posts: list[dict], snapshots: list[dict],
                     channel: str) -> dict | None:
    """Growth from dated follower observations. Needs 2+ distinct
    dates; a single observation is not growth."""
    by_date: dict[date, float] = {}
    for p in posts + snapshots:
        if "followers" in p:
            by_date[p["date"]] = float(p["followers"])
    dates = sorted(by_date)
    if len(dates) < 2:
        return None
    first, last = by_date[dates[0]], by_date[dates[-1]]
    days = max((dates[-1] - dates[0]).days, 1)
    change = last - first
    return {
        "channel": channel,
        "from": dates[0].isoformat(),
        "to": dates[-1].isoformat(),
        "from_followers": int(first),
        "to_followers": int(last),
        "change": int(change),
        "change_pct": round(change / first * 100, 1) if first else None,
        "per_day": round(change / days, 1),
        "series": [{"date": d.isoformat(), "followers": int(by_date[d])}
                   for d in dates],
    }


def _sentiment(posts: list[dict]) -> dict | None:
    """Keyword sentiment, not AI: count fixed word lists in captions
    and titles. None when there is no text at all."""
    positive = negative = 0
    for p in posts:
        words = _WORD_RE.findall((p.get("caption") or "").lower())
        for w in words:
            if w in POSITIVE_WORDS:
                positive += 1
            elif w in NEGATIVE_WORDS:
                negative += 1
    if positive == 0 and negative == 0:
        return None
    total = positive + negative
    if positive > negative * 2:
        label = "positive"
    elif negative > positive * 2:
        label = "negative"
    elif positive == negative:
        label = "neutral"
    else:
        label = "mixed"
    return {"method": "keyword sentiment, not AI",
            "positive": positive, "negative": negative,
            "net": positive - negative,
            "label": label}


def _recommendations(channels: dict, overall: dict, best_time: dict | None,
                     growth: dict, sentiment: dict | None) -> list[str]:
    recs: list[str] = []
    rated = {n: s for n, s in channels.items()
             if s.get("engagement_rate") is not None and s["posts"] >= 3}
    overall_rate = overall.get("engagement_rate")
    if rated and overall_rate is not None:
        weakest = min(rated, key=lambda n: rated[n]["engagement_rate"])
        if rated[weakest]["engagement_rate"] < overall_rate:
            recs.append(
                f"{weakest} engages at "
                f"{rated[weakest]['engagement_rate'] * 100:.1f}% vs "
                f"{overall_rate * 100:.1f}% overall — rethink its format "
                f"or post less there until a format works.")
    if best_time and "hour" in best_time:
        recs.append(
            f"Post more around {best_time['hour']:02d}:00 — that hour "
            f"averages {best_time['hour_avg_interactions']} interactions "
            f"per post ({best_time['hour_posts']} posts).")
    for name, g in growth.items():
        if g["change"] <= 0:
            recs.append(
                f"Followers are flat or falling on {name} "
                f"({g['from_followers']} → {g['to_followers']} from "
                f"{g['from']} to {g['to']}) — check whether posting "
                f"frequency or content drifted in that window.")
            break
    if sentiment and sentiment["label"] in ("negative", "mixed") \
            and sentiment["negative"] >= 3:
        recs.append(
            "Caption language skews negative — read the lowest-performing "
            "posts and check whether the complaints repeat a theme.")
    if overall.get("engagement_rate_denominator") != "reach":
        recs.append(
            "Export reach (or impressions) with the next export so the "
            "engagement rate can use reach as its denominator instead "
            f"of {overall.get('engagement_rate_denominator')}.")
    return recs[:RECOMMENDATION_LIMIT]


def analyze(rows: list[dict], channel_filter: str | None = None) -> dict:
    """Rows from parse_export -> the full analytics result. Raises
    ValueError when the filter leaves zero usable rows."""
    posts = [r for r in rows if r["kind"] == "post"]
    snapshots = [r for r in rows if r["kind"] == "snapshot"]
    if channel_filter:
        want = channel_filter.strip().lower()
        posts = [p for p in posts if p["channel"] == want]
        snapshots = [s for s in snapshots if s["channel"] == want]
        if not posts and not snapshots:
            raise ValueError(f"no rows for channel '{channel_filter}'")
    channels: dict[str, dict] = {}
    for name in sorted({p["channel"] for p in posts + snapshots}):
        channels[name] = _channel_stats(
            [p for p in posts if p["channel"] == name],
            [s for s in snapshots if s["channel"] == name])
    overall = _channel_stats(posts, snapshots)
    result: dict = {
        "channels": channels,
        "overall": overall,
        "rows_parsed": len(rows),
        "top_posts": [],
    }
    ranked = sorted(posts, key=_interactions, reverse=True)[:5]
    for p in ranked:
        item = {"channel": p["channel"], "date": p["date"].isoformat(),
                "interactions": int(_interactions(p)),
                "likes": int(p.get("likes") or 0),
                "comments": int(p.get("comments") or 0),
                "shares": int(p.get("shares") or 0),
                "caption": (p.get("caption") or "")[:120]}
        if "post_id" in p:
            item["post_id"] = p["post_id"]
        result["top_posts"].append(item)
    best = _best_time(posts)
    if best is not None:
        result["best_time"] = best
        result["hourly_averages"] = _hourly_averages(posts)
    growth: dict[str, dict] = {}
    for name in channels:
        g = _follower_growth(
            [p for p in posts if p["channel"] == name],
            [s for s in snapshots if s["channel"] == name], name)
        if g is not None:
            growth[name] = g
    if growth:
        result["follower_growth"] = growth
    sentiment = _sentiment(posts)
    if sentiment is not None:
        result["sentiment"] = sentiment
    result["recommendations"] = _recommendations(
        channels, overall, best, growth, sentiment)
    unavailable: list[str] = []
    if overall.get("engagement_rate_denominator") != "reach":
        unavailable.append(
            "reach-based engagement rate (no reach in the export; "
            f"using {overall.get('engagement_rate_denominator')})")
    if best is None:
        unavailable.append("best posting time (no usable time data)")
    if not growth:
        unavailable.append(
            "follower growth (needs follower counts on 2+ dates)")
    if sentiment is None:
        unavailable.append("sentiment (no caption text in the export)")
    if unavailable:
        result["unavailable"] = unavailable
    return result


# -- vector charts (hand-built SVG, no dependencies) --------------------------

def _esc(text) -> str:
    return _xml_escape(str(text))


def _svg_open(width: int, height: int, title: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img"><title>{_esc(title)}</title>')


def bar_chart_engagement(channels: dict) -> str | None:
    """Vertical bars: engagement rate % per channel. None when no
    channel has a rate."""
    rated = [(n, s["engagement_rate"] * 100) for n, s in
             sorted(channels.items())
             if s.get("engagement_rate") is not None]
    if not rated:
        return None
    width, height = 640, 360
    pad_l, pad_b, pad_t, pad_r = 56, 56, 28, 16
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    top = max(v for _, v in rated) * 1.15 or 1
    parts = [_svg_open(width, height,
                       "Engagement rate by channel (percent)"),
             f'<rect width="{width}" height="{height}" fill="#FFFFFF"/>',
             f'<text x="{width // 2}" y="18" text-anchor="middle" '
             f'font-family="sans-serif" font-size="14" font-weight="bold" '
             f'fill="#111111">Engagement rate by channel</text>']
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + plot_h * (1 - frac)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" '
            f'y2="{y:.1f}" stroke="#E5E7EB"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-family="sans-serif" font-size="11" fill="#5A5A5A">'
            f'{top * frac:.1f}%</text>')
    slot = plot_w / len(rated)
    bar_w = min(slot * 0.6, 72)
    for i, (name, value) in enumerate(rated):
        bar_h = plot_h * value / top
        x = pad_l + slot * i + (slot - bar_w) / 2
        y = pad_t + plot_h - bar_h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" fill="#6D28D9" rx="3">'
            f'<title>{_esc(name)}: {value:.1f}%</title></rect>'
            f'<text x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" '
            f'text-anchor="middle" font-family="sans-serif" font-size="12" '
            f'font-weight="bold" fill="#111111">{value:.1f}%</text>'
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 34}" '
            f'text-anchor="middle" font-family="sans-serif" font-size="12" '
            f'fill="#111111">{_esc(name[:14])}</text>')
    parts.append(
        f'<text x="{width // 2}" y="{height - 12}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="11" fill="#5A5A5A">'
        f'Engagement rate = interactions / reach (or followers when '
        f'reach is missing)</text></svg>')
    return "".join(parts)


def bar_chart_posting_time(hourly: list[dict], best_hour: int | None) -> str | None:
    """Bars: average interactions per posting hour. The best hour is
    highlighted. None when there is no time data."""
    if not hourly:
        return None
    width, height = 640, 360
    pad_l, pad_b, pad_t, pad_r = 56, 56, 28, 16
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    top = max(h["avg_interactions"] for h in hourly) * 1.15 or 1
    parts = [_svg_open(width, height,
                       "Average interactions by posting hour"),
             f'<rect width="{width}" height="{height}" fill="#FFFFFF"/>',
             f'<text x="{width // 2}" y="18" text-anchor="middle" '
             f'font-family="sans-serif" font-size="14" font-weight="bold" '
             f'fill="#111111">Average interactions by posting hour</text>']
    for frac in (0, 0.5, 1.0):
        y = pad_t + plot_h * (1 - frac)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" '
            f'y2="{y:.1f}" stroke="#E5E7EB"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-family="sans-serif" font-size="11" fill="#5A5A5A">'
            f'{top * frac:.0f}</text>')
    slot = plot_w / len(hourly)
    bar_w = min(slot * 0.7, 40)
    for i, h in enumerate(hourly):
        bar_h = plot_h * h["avg_interactions"] / top
        x = pad_l + slot * i + (slot - bar_w) / 2
        y = pad_t + plot_h - bar_h
        fill = "#6D28D9" if h["hour"] == best_hour else "#C4B5FD"
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" fill="{fill}" rx="3">'
            f'<title>{h["hour"]:02d}:00 — {h["avg_interactions"]} avg '
            f'interactions ({h["posts"]} posts)</title></rect>'
            f'<text x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" '
            f'text-anchor="middle" font-family="sans-serif" font-size="11" '
            f'font-weight="bold" fill="#111111">'
            f'{h["avg_interactions"]:.0f}</text>'
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 34}" '
            f'text-anchor="middle" font-family="sans-serif" font-size="11" '
            f'fill="#111111">{h["hour"]:02d}:00</text>')
    parts.append(
        f'<text x="{width // 2}" y="{height - 12}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="11" fill="#5A5A5A">'
        f'Dark bar = best hour. Averages need 2+ posts per hour to '
        f'count.</text></svg>')
    return "".join(parts)


def line_chart_followers(growth: dict) -> str | None:
    """Lines: follower count over time, one line per channel."""
    series = [g for g in growth.values() if len(g.get("series", [])) >= 2]
    if not series:
        return None
    width, height = 640, 360
    pad_l, pad_b, pad_t, pad_r = 56, 56, 28, 16
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    all_dates = sorted({pt["date"] for g in series for pt in g["series"]})
    x_of = {d: pad_l + plot_w * i / max(len(all_dates) - 1, 1)
            for i, d in enumerate(all_dates)}
    values = [pt["followers"] for g in series for pt in g["series"]]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    lo -= span * 0.1
    hi += span * 0.1

    def y_of(v):
        return pad_t + plot_h * (1 - (v - lo) / (hi - lo))

    parts = [_svg_open(width, height, "Follower growth over time"),
             f'<rect width="{width}" height="{height}" fill="#FFFFFF"/>',
             f'<text x="{width // 2}" y="18" text-anchor="middle" '
             f'font-family="sans-serif" font-size="14" font-weight="bold" '
             f'fill="#111111">Follower growth</text>']
    for frac in (0, 0.5, 1.0):
        y = pad_t + plot_h * (1 - frac)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" '
            f'y2="{y:.1f}" stroke="#E5E7EB"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-family="sans-serif" font-size="11" fill="#5A5A5A">'
            f'{lo + (hi - lo) * frac:,.0f}</text>')
    for i, d in enumerate(all_dates):
        x = x_of[d]
        parts.append(
            f'<text x="{x:.1f}" y="{height - 34}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="11" fill="#111111">'
            f'{_esc(d[5:])}</text>')
    for idx, g in enumerate(series):
        color = CHANNEL_COLORS[idx % len(CHANNEL_COLORS)]
        points = " ".join(
            f"{x_of[pt['date']]:.1f},{y_of(pt['followers']):.1f}"
            for pt in g["series"])
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" '
            f'stroke-width="2.5"><title>{_esc(g["channel"])}: '
            f'{g["from_followers"]:,} → {g["to_followers"]:,} '
            f'({g["change"]:+d})</title></polyline>')
        for pt in g["series"]:
            parts.append(
                f'<circle cx="{x_of[pt["date"]]:.1f}" '
                f'cy="{y_of(pt["followers"]):.1f}" r="4" fill="{color}">'
                f'<title>{_esc(g["channel"])} {pt["date"]}: '
                f'{pt["followers"]:,}</title></circle>')
        lx = width - pad_r - 8
        ly = pad_t + 14 + idx * 20
        parts.append(
            f'<line x1="{lx - 90}" y1="{ly - 4}" x2="{lx - 70}" '
            f'y2="{ly - 4}" stroke="{color}" stroke-width="2.5"/>'
            f'<text x="{lx - 64}" y="{ly}" text-anchor="start" '
            f'font-family="sans-serif" font-size="11" fill="#111111">'
            f'{_esc(g["channel"][:16])}</text>')
    parts.append(
        f'<text x="{width // 2}" y="{height - 12}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="11" fill="#5A5A5A">'
        f'Follower counts from the export (snapshots or dated posts).'
        f'</text></svg>')
    return "".join(parts)


def build_charts(result: dict) -> dict:
    """All charts the result supports. Missing data -> the chart is
    omitted, never an empty placeholder."""
    charts: dict[str, str] = {}
    engagement = bar_chart_engagement(result["channels"])
    if engagement is not None:
        charts["engagement"] = engagement
    if "hourly_averages" in result:
        best_hour = result.get("best_time", {}).get("hour")
        posting = bar_chart_posting_time(result["hourly_averages"],
                                         best_hour)
        if posting is not None:
            charts["posting_time"] = posting
    if "follower_growth" in result:
        growth_chart = line_chart_followers(result["follower_growth"])
        if growth_chart is not None:
            charts["followers"] = growth_chart
    return charts


def fmt_int(value) -> str:
    return f"{int(value):,}" if value is not None else "—"


def fmt_pct(rate) -> str:
    return f"{rate * 100:.2f}%" if rate is not None else "unavailable"
