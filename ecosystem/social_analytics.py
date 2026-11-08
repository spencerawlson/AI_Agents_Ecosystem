"""Social media analytics program: turn a platform export into a
marketing-department report with graphs.

    python3 ecosystem/social_analytics.py report --data export.csv
    python3 ecosystem/social_analytics.py report --data export.json --channel instagram
    python3 ecosystem/social_analytics.py report --data export.csv --title "October social report"

Reads a CSV or JSON export (see docs/social-analytics.md for the
columns), analyses it with core/social_analytics.py, and writes
into reports/:

- social_<date>_<slug>.md — the report, linking to the SVG charts
- social_<date>_<slug>.html — the same report with the SVG charts
  embedded inline, so the graphs are visible when it is opened
- social_<date>_<slug>_engagement.svg — engagement rate by channel
- social_<date>_<slug>_posting_time.svg — average interactions by hour
- social_<date>_<slug>_followers.svg — follower growth over time

A chart is only written when the data supports it (no reach and no
followers means no engagement chart; no time data means no posting
time chart; fewer than two follower observations means no growth
chart). No network, no LLM, no spend — the input is a file the
owner exported from their own platform accounts.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.social_analytics import (  # noqa: E402
    analyze, build_charts, fmt_int, fmt_pct, parse_export,
)
from dashboard.reports import reports_dir  # noqa: E402

CHART_TITLES = {
    "engagement": "Engagement rate by channel",
    "posting_time": "Average interactions by posting hour",
    "followers": "Follower growth",
}

CHART_FILES = {
    "engagement": "engagement",
    "posting_time": "posting_time",
    "followers": "followers",
}


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "report"


def _pct(rate) -> str:
    return fmt_pct(rate)


def _channel_table(result: dict) -> list[str]:
    lines = ["| Channel | Posts | Interactions | Reach | Impressions | "
             "Followers | Engagement rate |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, s in result["channels"].items():
        lines.append(
            f"| {name} | {s['posts']} | {fmt_int(s['interactions'])} | "
            f"{fmt_int(s['reach'])} | {fmt_int(s['impressions'])} | "
            f"{fmt_int(s.get('followers'))} | "
            f"{_pct(s['engagement_rate'])} "
            f"({s['engagement_rate_denominator']}) |")
    return lines


def render_markdown(result: dict, title: str, source: str, now: datetime,
                    charts: dict, stem: str) -> str:
    overall = result["overall"]
    lines = [f"# {title}", "",
             f"_Generated {now:%Y-%m-%d %H:%M} UTC from `{source}` — "
             f"{result['rows_parsed']} rows parsed. This report is built "
             f"from a platform export, not a live API._", "",
             "## Summary", "",
             f"- **Posts analysed:** {fmt_int(overall['posts'])}",
             f"- **Interactions:** {fmt_int(overall['interactions'])} "
             f"(likes {fmt_int(overall['likes'])}, comments "
             f"{fmt_int(overall['comments'])}, shares "
             f"{fmt_int(overall['shares'])}, saves "
             f"{fmt_int(overall['saves'])})",
             f"- **Reach:** {fmt_int(overall['reach'])} · "
             f"**Impressions:** {fmt_int(overall['impressions'])}",
             f"- **Engagement rate:** {_pct(overall['engagement_rate'])} "
             f"(denominator: {overall['engagement_rate_denominator']})",
             f"- **Followers (latest):** "
             f"{fmt_int(overall.get('followers'))}", "",
             "## By channel", "",
             *_channel_table(result), ""]
    if charts:
        lines += ["## Graphs", ""]
        for key in ("engagement", "posting_time", "followers"):
            if key in charts:
                fname = f"{stem}_{CHART_FILES[key]}.svg"
                lines.append(f"![{CHART_TITLES[key]}]({fname})")
                lines.append("")
    if result["top_posts"]:
        lines += ["## Top posts", "",
                  "| Date | Channel | Interactions | Likes | Comments | "
                  "Shares | Caption |",
                  "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
        for p in result["top_posts"]:
            caption = (p["caption"] or "").replace("|", "/")
            lines.append(
                f"| {p['date']} | {p['channel']} | "
                f"{fmt_int(p['interactions'])} | {fmt_int(p['likes'])} | "
                f"{fmt_int(p['comments'])} | {fmt_int(p['shares'])} | "
                f"{caption} |")
        lines.append("")
    if "best_time" in result:
        best = result["best_time"]
        lines += ["## Best time to post", ""]
        if "hour" in best:
            lines.append(
                f"- **Best hour:** {best['hour']:02d}:00 — "
                f"{best['hour_avg_interactions']} average interactions "
                f"per post ({best['hour_posts']} posts).")
        if "weekday" in best:
            lines.append(
                f"- **Best weekday:** {best['weekday']} — "
                f"{best['weekday_avg_interactions']} average interactions "
                f"per post ({best['weekday_posts']} posts).")
        lines.append("")
    if "follower_growth" in result:
        lines += ["## Follower growth", "",
                  "| Channel | From | To | Change | Per day |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for name, g in result["follower_growth"].items():
            pct = (f"{g['change_pct']:+.1f}%"
                   if g["change_pct"] is not None else "—")
            lines.append(
                f"| {name} | {fmt_int(g['from_followers'])} "
                f"({g['from']}) | {fmt_int(g['to_followers'])} "
                f"({g['to']}) | {g['change']:+d} ({pct}) | "
                f"{g['per_day']:+.1f} |")
        lines.append("")
    if "sentiment" in result:
        s = result["sentiment"]
        lines += ["## Sentiment — keyword sentiment, not AI", "",
                  f"- Positive words: {s['positive']} · "
                  f"Negative words: {s['negative']} · Net: {s['net']:+d}",
                  f"- Overall tone: **{s['label']}**",
                  "- Method: a fixed positive/negative word list counted "
                  "in captions and titles. This is keyword counting, not "
                  "an AI reading the comments — treat it as a smoke "
                  "signal, not a verdict.", ""]
    if result["recommendations"]:
        lines += ["## Recommendations", ""]
        for rec in result["recommendations"]:
            lines.append(f"- {rec}")
        lines.append("")
    lines += ["## What this report cannot tell you", "",
              "- It reads an export, not a live API — numbers are only as "
              "fresh as the file.",
              "- Engagement rates use reach when the export has it, "
              "followers when it does not; the denominator is printed "
              "next to every rate.",
              "- Sentiment is keyword counting in captions, not an AI "
              "reading comments.",
              "- It cannot see competitors, ad spend, or why a post "
              "worked — only what the export recorded."]
    if result.get("unavailable"):
        lines.append("")
        lines.append("Unavailable in this export:")
        for item in result["unavailable"]:
            lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def render_html(result: dict, title: str, source: str, now: datetime,
                charts: dict) -> str:
    """Standalone HTML with the SVG charts embedded inline."""
    overall = result["overall"]
    esc = html.escape
    parts = [
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{esc(title)}</title><style>"
        "body{font-family:sans-serif;max-width:860px;margin:2rem auto;"
        "padding:0 1rem;color:#111}h1{font-size:1.6rem}h2{margin-top:2rem;"
        "border-bottom:2px solid #C4B5FD;padding-bottom:.3rem}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0}"
        "th,td{border:1px solid #E5E7EB;padding:.4rem .6rem;text-align:left}"
        "th{background:#F3E8FF}td.num,th.num{text-align:right}"
        ".meta{color:#5A5A5A}.chart{margin:1.5rem 0}"
        ".chart svg{max-width:100%;height:auto}"
        "li{margin:.3rem 0}</style></head><body>",
        f"<h1>{esc(title)}</h1>",
        f"<p class=\"meta\">Generated {now:%Y-%m-%d %H:%M} UTC from "
        f"{esc(source)} — {result['rows_parsed']} rows parsed. Built from "
        f"a platform export, not a live API.</p>",
        "<h2>Summary</h2><ul>",
        f"<li><strong>Posts analysed:</strong> {fmt_int(overall['posts'])}</li>",
        f"<li><strong>Interactions:</strong> {fmt_int(overall['interactions'])} "
        f"(likes {fmt_int(overall['likes'])}, comments "
        f"{fmt_int(overall['comments'])}, shares "
        f"{fmt_int(overall['shares'])}, saves "
        f"{fmt_int(overall['saves'])})</li>",
        f"<li><strong>Reach:</strong> {fmt_int(overall['reach'])} · "
        f"<strong>Impressions:</strong> {fmt_int(overall['impressions'])}</li>",
        f"<li><strong>Engagement rate:</strong> {_pct(overall['engagement_rate'])} "
        f"(denominator: {esc(overall['engagement_rate_denominator'])})</li>",
        f"<li><strong>Followers (latest):</strong> "
        f"{fmt_int(overall.get('followers'))}</li></ul>",
        "<h2>By channel</h2><table><tr><th>Channel</th>"
        "<th class=\"num\">Posts</th><th class=\"num\">Interactions</th>"
        "<th class=\"num\">Reach</th><th class=\"num\">Impressions</th>"
        "<th class=\"num\">Followers</th><th>Engagement rate</th></tr>",
    ]
    for name, s in result["channels"].items():
        parts.append(
            f"<tr><td>{esc(name)}</td>"
            f"<td class=\"num\">{s['posts']}</td>"
            f"<td class=\"num\">{fmt_int(s['interactions'])}</td>"
            f"<td class=\"num\">{fmt_int(s['reach'])}</td>"
            f"<td class=\"num\">{fmt_int(s['impressions'])}</td>"
            f"<td class=\"num\">{fmt_int(s.get('followers'))}</td>"
            f"<td>{_pct(s['engagement_rate'])} "
            f"({esc(s['engagement_rate_denominator'])})</td></tr>")
    parts.append("</table>")
    for key in ("engagement", "posting_time", "followers"):
        if key in charts:
            parts.append(f"<h2>{esc(CHART_TITLES[key])}</h2>"
                         f"<div class=\"chart\">{charts[key]}</div>")
    if result["top_posts"]:
        parts.append("<h2>Top posts</h2><table><tr><th>Date</th>"
                     "<th>Channel</th><th class=\"num\">Interactions</th>"
                     "<th class=\"num\">Likes</th><th class=\"num\">Comments</th>"
                     "<th class=\"num\">Shares</th><th>Caption</th></tr>")
        for p in result["top_posts"]:
            parts.append(
                f"<tr><td>{esc(p['date'])}</td><td>{esc(p['channel'])}</td>"
                f"<td class=\"num\">{fmt_int(p['interactions'])}</td>"
                f"<td class=\"num\">{fmt_int(p['likes'])}</td>"
                f"<td class=\"num\">{fmt_int(p['comments'])}</td>"
                f"<td class=\"num\">{fmt_int(p['shares'])}</td>"
                f"<td>{esc(p['caption'] or '')}</td></tr>")
        parts.append("</table>")
    if "best_time" in result:
        best = result["best_time"]
        parts.append("<h2>Best time to post</h2><ul>")
        if "hour" in best:
            parts.append(
                f"<li><strong>Best hour:</strong> {best['hour']:02d}:00 — "
                f"{best['hour_avg_interactions']} average interactions "
                f"per post ({best['hour_posts']} posts).</li>")
        if "weekday" in best:
            parts.append(
                f"<li><strong>Best weekday:</strong> {esc(best['weekday'])} — "
                f"{best['weekday_avg_interactions']} average interactions "
                f"per post ({best['weekday_posts']} posts).</li>")
        parts.append("</ul>")
    if "follower_growth" in result:
        parts.append("<h2>Follower growth</h2><table><tr><th>Channel</th>"
                     "<th class=\"num\">From</th><th class=\"num\">To</th>"
                     "<th class=\"num\">Change</th><th class=\"num\">Per day</th></tr>")
        for name, g in result["follower_growth"].items():
            pct = (f"{g['change_pct']:+.1f}%"
                   if g["change_pct"] is not None else "—")
            parts.append(
                f"<tr><td>{esc(name)}</td>"
                f"<td class=\"num\">{fmt_int(g['from_followers'])} "
                f"({esc(g['from'])})</td>"
                f"<td class=\"num\">{fmt_int(g['to_followers'])} "
                f"({esc(g['to'])})</td>"
                f"<td class=\"num\">{g['change']:+d} ({pct})</td>"
                f"<td class=\"num\">{g['per_day']:+.1f}</td></tr>")
        parts.append("</table>")
    if "sentiment" in result:
        s = result["sentiment"]
        parts.append("<h2>Sentiment — keyword sentiment, not AI</h2><ul>"
                     f"<li>Positive words: {s['positive']} · Negative words: "
                     f"{s['negative']} · Net: {s['net']:+d}</li>"
                     f"<li>Overall tone: <strong>{esc(s['label'])}</strong></li>"
                     "<li>Method: a fixed positive/negative word list "
                     "counted in captions and titles. Keyword counting, "
                     "not an AI reading the comments.</li></ul>")
    if result["recommendations"]:
        parts.append("<h2>Recommendations</h2><ul>")
        for rec in result["recommendations"]:
            parts.append(f"<li>{esc(rec)}</li>")
        parts.append("</ul>")
    parts.append("<h2>What this report cannot tell you</h2><ul>"
                 "<li>It reads an export, not a live API — numbers are only "
                 "as fresh as the file.</li>"
                 "<li>Engagement rates use reach when the export has it, "
                 "followers when it does not; the denominator is printed "
                 "next to every rate.</li>"
                 "<li>Sentiment is keyword counting in captions, not an AI "
                 "reading comments.</li>"
                 "<li>It cannot see competitors, ad spend, or why a post "
                 "worked — only what the export recorded.</li></ul>")
    if result.get("unavailable"):
        parts.append("<p><strong>Unavailable in this export:</strong></p><ul>")
        for item in result["unavailable"]:
            parts.append(f"<li>{esc(item)}</li>")
        parts.append("</ul>")
    parts.append("</body></html>")
    return "\n".join(parts)


def run_report(data_path: Path, channel: str | None, title: str | None,
               reports_root: Path | None = None,
               now: datetime | None = None) -> dict:
    """Parse + analyse + write the report files. Returns the paths
    written and the result. Raises ValueError on an unusable export."""
    now = now or datetime.now(timezone.utc)
    reports_root = reports_root or reports_dir()
    text = data_path.read_text(encoding="utf-8-sig")
    rows, skipped = parse_export(text, filename=data_path.name)
    result = analyze(rows, channel_filter=channel)
    charts = build_charts(result)
    label = channel or "all-channels"
    stem = f"social_{now:%Y%m%d}_{_slug(label)}"
    title = title or f"Social media report — {label} ({now:%Y-%m-%d})"
    reports_root.mkdir(parents=True, exist_ok=True)
    md_path = reports_root / f"{stem}.md"
    md_path.write_text(
        render_markdown(result, title, data_path.name, now, charts, stem),
        encoding="utf-8")
    html_path = reports_root / f"{stem}.html"
    html_path.write_text(
        render_html(result, title, data_path.name, now, charts),
        encoding="utf-8")
    chart_paths = {}
    for key, svg in charts.items():
        path = reports_root / f"{stem}_{CHART_FILES[key]}.svg"
        path.write_text(svg, encoding="utf-8")
        chart_paths[key] = path
    return {"result": result, "skipped": skipped, "md": md_path,
            "html": html_path, "charts": chart_paths}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Social media analytics: turn a platform export "
                    "into a marketing report with graphs.")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("report", help="build a report from an export file")
    p.add_argument("--data", required=True,
                   help="export file (.csv or .json)")
    p.add_argument("--channel", default=None,
                   help="analyse one channel only (e.g. instagram)")
    p.add_argument("--title", default=None, help="report title")
    args = parser.parse_args(argv)
    if args.cmd != "report":
        parser.print_help()
        return 2
    data_path = Path(args.data)
    if not data_path.is_file():
        print(f"error: no such file: {data_path}")
        return 1
    try:
        out = run_report(data_path, args.channel, args.title)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    result = out["result"]
    overall = result["overall"]
    print(f"posts: {overall['posts']} across "
          f"{len(result['channels'])} channel(s)")
    print(f"engagement rate: {_pct(overall['engagement_rate'])} "
          f"({overall['engagement_rate_denominator']})")
    print(f"report: {out['md']}")
    print(f"html: {out['html']}")
    for key, path in out["charts"].items():
        print(f"chart ({CHART_TITLES[key]}): {path}")
    if out["skipped"]:
        print(f"skipped rows: {out['skipped']}")
    if result.get("unavailable"):
        for item in result["unavailable"]:
            print(f"unavailable: {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
