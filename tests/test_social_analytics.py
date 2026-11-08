"""Social media analytics: parsing (CSV + JSON), every metric, the
unavailable-data paths, the SVG charts, and the CLI end to end.

No network, no LLM, no spend anywhere here — every export is a
fake string in this file.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.social_analytics import (
    analyze, bar_chart_engagement, bar_chart_posting_time,
    build_charts, line_chart_followers, parse_export,
)
import ecosystem.social_analytics as program

CSV = """date,time,channel,post_id,caption,post_type,likes,comments,shares,saves,reach,impressions,views,followers,row_type
2026-11-02,09:00,instagram,ig1,Amazing new drop love it!,photo,100,10,5,0,1000,1200,,,post
2026-11-09,09:00,instagram,ig2,Great photos thank you all,photo,120,12,8,0,1100,1300,,,post
2026-11-03,18:00,instagram,ig3,New stock arrived,reel,20,2,0,0,500,600,,,post
2026-11-10,18:00,instagram,ig4,Behind the scenes,photo,25,3,1,0,550,700,,,post
2026-11-02,12:00,tiktok,tt1,Watch this amazing trick,video,300,20,40,0,,5000,5000,,post
2026-11-09,12:00,tiktok,tt2,Terrible delay awful service sorry,video,280,18,35,0,,4800,4800,,post
2026-11-03,12:00,tiktok,tt3,Quick tip of the day,video,50,5,5,0,,900,900,,post
2026-11-10,12:00,tiktok,tt4,Part two of the series,video,45,4,4,0,,850,850,,post
2026-11-01,,instagram,,,,,,,,,,,5000,snapshot
2026-11-10,,instagram,,,,,,,,,,,5200,snapshot
2026-11-01,,tiktok,,,,,,,,,,,800,snapshot
2026-11-10,,tiktok,,,,,,,,,,,750,snapshot
"""

JSON_LIST = """[{"date": "2026-11-02", "time": "09:00", "channel": "instagram",
"caption": "Amazing launch", "likes": 100, "comments": 10, "reach": 1000},
{"date": "2026-11-03", "time": "18:00", "channel": "instagram",
"caption": "Quiet day", "likes": 20, "comments": 2, "reach": 500}]"""


# -- parsing ------------------------------------------------------------------

def test_parse_csv_counts_posts_and_snapshots():
    rows, skipped = parse_export(CSV, "export.csv")
    assert skipped == 0
    assert len([r for r in rows if r["kind"] == "post"]) == 8
    assert len([r for r in rows if r["kind"] == "snapshot"]) == 4
    assert rows[0]["channel"] == "instagram"
    assert rows[0]["hour"] == 9
    assert rows[4]["channel"] == "tiktok"


def test_parse_csv_number_suffixes_and_commas():
    csv_text = ("date,channel,likes,comments,reach\n"
                "2026-11-02,instagram,\"1,234\",5,2.5k\n"
                "2026-11-03,instagram,10,1,1.2m\n")
    rows, _ = parse_export(csv_text, "export.csv")
    assert rows[0]["likes"] == 1234
    assert rows[0]["reach"] == 2500
    assert rows[1]["reach"] == 1200000


def test_parse_csv_skips_bad_rows_and_counts_them():
    csv_text = ("date,channel,likes,comments,reach\n"
                "not-a-date,instagram,10,1,100\n"
                ",instagram,10,1,100\n"
                "2026-11-02,instagram,10,1,100\n")
    rows, skipped = parse_export(csv_text, "export.csv")
    assert len(rows) == 1
    assert skipped == 2


def test_parse_csv_empty_raises():
    with pytest.raises(ValueError, match="no usable rows"):
        parse_export("date,channel,likes\n", "export.csv")
    with pytest.raises(ValueError, match="no usable rows"):
        parse_export("", "export.csv")


def test_parse_unknown_extension_raises():
    with pytest.raises(ValueError, match="unknown export format"):
        parse_export(CSV, "export.xlsx")


def test_parse_json_list_and_object_shapes():
    rows, skipped = parse_export(JSON_LIST, "export.json")
    assert len(rows) == 2
    assert skipped == 0
    rows2, _ = parse_export(
        '{"posts": [{"date": "2026-11-02", "channel": "x", "likes": 5}],'
        ' "snapshots": [{"date": "2026-11-01", "channel": "x",'
        ' "followers": 100}, {"date": "2026-11-05", "channel": "x",'
        ' "followers": 120}]}', "export.json")
    assert len([r for r in rows2 if r["kind"] == "post"]) == 1
    assert len([r for r in rows2 if r["kind"] == "snapshot"]) == 2


def test_parse_json_invalid_raises():
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_export("{not json", "export.json")


# -- metrics ------------------------------------------------------------------

def _result():
    rows, _ = parse_export(CSV, "export.csv")
    return analyze(rows)


def test_per_channel_aggregates():
    result = _result()
    ig = result["channels"]["instagram"]
    assert ig["posts"] == 4
    assert ig["likes"] == 265
    assert ig["interactions"] == 306
    assert ig["reach"] == 3150
    assert ig["followers"] == 5200
    tt = result["channels"]["tiktok"]
    assert tt["posts"] == 4
    assert tt["interactions"] == 806
    assert tt["followers"] == 750


def test_engagement_rate_denominators():
    result = _result()
    ig = result["channels"]["instagram"]
    assert ig["engagement_rate_denominator"] == "reach"
    assert ig["engagement_rate"] == pytest.approx(306 / 3150, abs=0.0001)
    tt = result["channels"]["tiktok"]
    # No reach on TikTok: falls back to followers, and says so.
    assert tt["engagement_rate_denominator"] == "followers"
    assert tt["engagement_rate"] == pytest.approx(806 / 750, abs=0.0001)
    overall = result["overall"]
    assert overall["engagement_rate_denominator"] == "reach"
    assert overall["engagement_rate"] == pytest.approx(1112 / 3150,
                                                       abs=0.0001)


def test_engagement_rate_unavailable_without_reach_or_followers():
    csv_text = ("date,channel,likes,comments\n"
                "2026-11-02,x,10,2\n2026-11-03,x,5,1\n")
    rows, _ = parse_export(csv_text, "export.csv")
    result = analyze(rows)
    assert result["overall"]["engagement_rate"] is None
    assert result["overall"]["engagement_rate_denominator"] == "unavailable"
    assert any("reach-based engagement rate" in u
               for u in result["unavailable"])


def test_top_posts_ranked_by_interactions():
    result = _result()
    top = result["top_posts"]
    assert top[0]["post_id"] == "tt1"
    assert top[0]["interactions"] == 360
    assert top[1]["post_id"] == "tt2"
    assert len(top) == 5  # capped at 5


def test_best_time_needs_two_posts_per_bucket():
    result = _result()
    best = result["best_time"]
    assert best["hour"] == 12
    assert best["hour_avg_interactions"] == pytest.approx(201.5)
    assert best["weekday"] == "Monday"
    assert best["weekday_avg_interactions"] == pytest.approx(237.0)


def test_best_time_omitted_without_time_data():
    csv_text = ("date,channel,likes,comments,reach\n"
                "2026-11-02,instagram,10,1,100\n"
                "2026-11-03,instagram,20,2,200\n")
    rows, _ = parse_export(csv_text, "export.csv")
    result = analyze(rows)
    assert "best_time" not in result
    assert "hourly_averages" not in result
    assert any("best posting time" in u for u in result["unavailable"])


def test_best_time_single_post_buckets_do_not_win():
    csv_text = ("date,time,channel,likes,comments,reach\n"
                "2026-11-02,09:00,instagram,1000,100,5000\n"
                "2026-11-03,18:00,instagram,10,1,500\n"
                "2026-11-10,18:00,instagram,12,1,500\n")
    rows, _ = parse_export(csv_text, "export.csv")
    result = analyze(rows)
    # 09:00 has one (viral) post; 18:00 has two. 18:00 wins.
    assert result["best_time"]["hour"] == 18


def test_follower_growth():
    result = _result()
    growth = result["follower_growth"]
    assert growth["instagram"]["change"] == 200
    assert growth["instagram"]["change_pct"] == pytest.approx(4.0)
    assert growth["instagram"]["per_day"] == pytest.approx(200 / 9, abs=0.1)
    assert growth["tiktok"]["change"] == -50
    assert len(growth["instagram"]["series"]) == 2


def test_follower_growth_omitted_with_one_observation():
    csv_text = ("date,channel,likes,followers\n"
                "2026-11-02,instagram,10,5000\n")
    rows, _ = parse_export(csv_text, "export.csv")
    result = analyze(rows)
    assert "follower_growth" not in result
    assert any("follower growth" in u for u in result["unavailable"])


def test_sentiment_keyword_not_ai():
    result = _result()
    s = result["sentiment"]
    assert s["method"] == "keyword sentiment, not AI"
    assert s["positive"] == 5  # amazing x2, love, great, thank
    assert s["negative"] == 2  # terrible, awful
    assert s["label"] == "positive"


def test_sentiment_omitted_without_text():
    csv_text = ("date,channel,likes,comments,reach\n"
                "2026-11-02,instagram,10,1,100\n")
    rows, _ = parse_export(csv_text, "export.csv")
    result = analyze(rows)
    assert "sentiment" not in result
    assert any("sentiment" in u for u in result["unavailable"])


def test_recommendations_are_deterministic_rules():
    result = _result()
    recs = result["recommendations"]
    assert 1 <= len(recs) <= 5
    assert any("instagram engages at 9.7%" in r for r in recs)
    assert any("12:00" in r for r in recs)
    assert any("tiktok" in r.lower() and "flat or falling" in r
               for r in recs)


def test_channel_filter():
    rows, _ = parse_export(CSV, "export.csv")
    result = analyze(rows, channel_filter="tiktok")
    assert list(result["channels"]) == ["tiktok"]
    assert result["overall"]["posts"] == 4
    with pytest.raises(ValueError, match="no rows for channel"):
        analyze(rows, channel_filter="linkedin")


# -- SVG charts ---------------------------------------------------------------

def test_engagement_chart_is_valid_svg_with_real_values():
    result = _result()
    svg = bar_chart_engagement(result["channels"])
    assert svg is not None
    assert "<image" not in svg  # vector only, never raster
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    text = " ".join(e.text or "" for e in root.iter())
    assert "instagram" in text and "tiktok" in text
    assert "9.7%" in text and "107.5%" in text
    rects = [e for e in root.iter() if e.tag.endswith("rect")]
    bars = [r for r in rects if r.get("fill") == "#6D28D9"]
    assert len(bars) == 2
    heights = sorted(float(b.get("height")) for b in bars)
    assert heights[1] > heights[0]  # taller bar = higher rate


def test_engagement_chart_none_without_rates():
    csv_text = ("date,channel,likes\n"
                "2026-11-02,x,10\n2026-11-03,x,5\n")
    rows, _ = parse_export(csv_text, "export.csv")
    assert bar_chart_engagement(analyze(rows)["channels"]) is None


def test_posting_time_chart_highlights_best_hour():
    result = _result()
    svg = bar_chart_posting_time(result["hourly_averages"],
                                 result["best_time"]["hour"])
    assert svg is not None
    root = ET.fromstring(svg)
    text = " ".join(e.text or "" for e in root.iter())
    assert "12:00" in text
    dark = [e for e in root.iter()
            if e.tag.endswith("rect") and e.get("fill") == "#6D28D9"]
    assert len(dark) == 1  # exactly the best hour is dark
    assert bar_chart_posting_time([], None) is None


def test_follower_chart_is_valid_svg_with_lines():
    result = _result()
    svg = line_chart_followers(result["follower_growth"])
    assert svg is not None
    root = ET.fromstring(svg)
    lines = [e for e in root.iter() if e.tag.endswith("polyline")]
    assert len(lines) == 2  # one per channel
    points = [e for e in root.iter() if e.tag.endswith("circle")]
    assert len(points) == 4  # two observations per channel
    assert line_chart_followers({}) is None


def test_build_charts_omits_what_data_does_not_support():
    csv_text = ("date,channel,likes\n"
                "2026-11-02,x,10\n2026-11-03,x,5\n")
    rows, _ = parse_export(csv_text, "export.csv")
    assert build_charts(analyze(rows)) == {}
    assert len(build_charts(_result())) == 3


# -- CLI ----------------------------------------------------------------------

def _write_csv(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text(CSV, encoding="utf-8")
    return path


def test_cli_report_writes_md_html_and_svgs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    data = _write_csv(tmp_path)
    assert program.main(["report", "--data", str(data)]) == 0
    out = capsys.readouterr().out
    assert "engagement rate: 35.30% (reach)" in out
    reports = tmp_path / "reports"
    files = sorted(p.name for p in reports.iterdir())
    assert any(f.endswith(".md") for f in files)
    assert any(f.endswith(".html") for f in files)
    assert sum(f.endswith(".svg") for f in files) == 3
    md = next(p for p in reports.iterdir() if p.suffix == ".md")
    md_text = md.read_text(encoding="utf-8")
    assert "![Engagement rate by channel]" in md_text
    assert "_engagement.svg" in md_text
    assert "_posting_time.svg" in md_text
    assert "_followers.svg" in md_text
    assert "keyword sentiment, not AI" in md_text
    html = next(p for p in reports.iterdir() if p.suffix == ".html")
    html_text = html.read_text(encoding="utf-8")
    assert "<svg" in html_text  # charts embedded inline
    assert "<image" not in html_text
    assert "Follower growth" in html_text


def test_cli_channel_filter(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    data = _write_csv(tmp_path)
    assert program.main(["report", "--data", str(data),
                         "--channel", "instagram"]) == 0
    md = next(p for p in (tmp_path / "reports").iterdir()
              if p.suffix == ".md")
    assert "tiktok" not in md.read_text(encoding="utf-8").lower()
    assert "instagram" in md.read_text(encoding="utf-8").lower()


def test_cli_bad_export_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    bad = tmp_path / "empty.csv"
    bad.write_text("date,channel,likes\n", encoding="utf-8")
    assert program.main(["report", "--data", str(bad)]) == 1
    assert "error:" in capsys.readouterr().out


def test_cli_missing_file_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    assert program.main(["report", "--data",
                         str(tmp_path / "nope.csv")]) == 1
    assert "error: no such file" in capsys.readouterr().out


def test_cli_no_command_prints_help(tmp_path, capsys):
    assert program.main([]) == 2
