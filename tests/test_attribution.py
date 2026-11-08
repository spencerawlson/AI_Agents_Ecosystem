"""Funnel attribution: channel classification, touch models,
aggregation, suggestions, the record-conversion / attribution CLI
and the dashboard endpoint.

No network, no LLM, no spend anywhere in this file — Plausible is
patched out, conversions are recorded by hand in tmp state.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.attribution import (
    aggregate, build_attribution, classify_source, classify_touch,
    parse_touches, parse_utm, sessions_from_snapshot, suggest,
)
import ecosystem.road2cissp_growth as growth
from ecosystem.road2cissp_growth import load_state, save_state

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
SITE = "https://road2cissp.com"


def _conv(amount, touches):
    return {"id": "conv_1", "at": NOW.isoformat(), "amount_usd": amount,
            "touches": touches}


# -- classification -----------------------------------------------------------

def test_classify_source_by_name():
    assert classify_source("Google") == "seo"
    assert classify_source("Bing") == "seo"
    assert classify_source("DuckDuckGo") == "seo"
    assert classify_source("reddit") == "community"
    assert classify_source("r/cissp") == "community"
    assert classify_source("twitter") == "social"
    assert classify_source("x.com") == "social"
    assert classify_source("threads") == "social"
    assert classify_source("linkedin") == "social"
    assert classify_source("") == "direct"
    assert classify_source(None) == "direct"
    assert classify_source("some-blog.example") == "referral"
    assert classify_source("road2cissp.com") == "direct"


def test_classify_source_utm_medium_wins():
    assert classify_source("Google", medium="cpc") == "paid"
    assert classify_source("reddit", medium="social") == "social"
    assert classify_source("Google", medium="organic") == "seo"
    assert classify_source("newsletter", medium="email") == "email"
    assert classify_source("Google", medium="none") == "direct"


def test_parse_utm_and_classify_from_landing_url():
    url = f"{SITE}/pricing?utm_source=reddit&utm_medium=social&utm_campaign=answers"
    assert parse_utm(url) == {"source": "reddit", "medium": "social",
                              "campaign": "answers"}
    assert classify_source(None, landing_url=url) == "social"
    assert parse_utm(f"{SITE}/pricing") == {}
    assert parse_utm("not a url") == {}


def test_classify_touch_explicit_channel_wins():
    assert classify_touch({"channel": "community", "source": "google"}) \
        == "community"
    assert classify_touch({"source": "google"}) == "seo"


def test_parse_touches_format_and_errors():
    touches = parse_touches("seo:google, community:r/cissp")
    assert touches == [{"channel": "seo", "source": "google"},
                       {"channel": "community", "source": "r/cissp"}]
    assert parse_touches("direct") == [{"channel": "direct"}]
    with pytest.raises(ValueError, match="unknown channel"):
        parse_touches("telepathy")
    with pytest.raises(ValueError, match="at least one touch"):
        parse_touches("")


# -- models and aggregation -----------------------------------------------------

def test_models_split_credit_correctly():
    conv = _conv(19.0, [{"channel": "seo", "source": "google"},
                        {"channel": "community", "source": "r/cissp"}])
    first = aggregate([conv], None, model="first_touch")
    assert {r["channel"]: r["revenue"] for r in first["channels"]} == {
        "seo": 19.0}
    last = aggregate([conv], None, model="last_touch")
    assert {r["channel"]: r["revenue"] for r in last["channels"]} == {
        "community": 19.0}
    linear = aggregate([conv], None, model="linear")
    assert {r["channel"]: (r["revenue"], r["conversions"])
            for r in linear["channels"]} == {
        "community": (9.5, 0.5), "seo": (9.5, 0.5)}
    assert linear["totals"] == {"sessions": None, "conversions": 1.0,
                                "revenue": 19.0, "conversion_rate": None}
    with pytest.raises(ValueError, match="unknown model"):
        aggregate([conv], None, model="telepathy")


def test_aggregate_rates_and_unmeasured_sessions():
    convs = [_conv(19.0, [{"channel": "seo"}]),
             _conv(19.0, [{"channel": "community"}]),
             _conv(19.0, [{"channel": "community"}])]
    out = aggregate(convs, {"seo": 100, "community": 50})
    by_channel = {r["channel"]: r for r in out["channels"]}
    assert by_channel["seo"]["conversion_rate"] == pytest.approx(0.01)
    assert by_channel["community"]["conversion_rate"] == pytest.approx(0.04)
    assert by_channel["community"]["revenue_per_session"] == pytest.approx(0.76)
    assert out["totals"]["conversion_rate"] == pytest.approx(0.02)
    # No sessions at all: rates are None, never invented.
    out2 = aggregate(convs, None)
    assert all(r["conversion_rate"] is None for r in out2["channels"])
    assert out2["totals"]["conversion_rate"] is None
    # A channel with sessions but no conversions still appears.
    out3 = aggregate(convs, {"seo": 100, "social": 200})
    assert {r["channel"]: r["conversions"] for r in out3["channels"]}["social"] == 0


def test_suggest_rules():
    assert suggest(aggregate([], None))[0]["channel"] is None
    assert "record-conversion" in suggest(aggregate([], None))[0]["suggestion"]
    out = aggregate([_conv(19.0, [{"channel": "community"}]),
                     _conv(19.0, [{"channel": "community"}])],
                    {"community": 40, "social": 100})
    suggestions = suggest(out)
    assert any(s["channel"] == "social" and "0 conversions" in s["signal"]
               for s in suggestions)
    assert any(s["channel"] == "community" and "converts best" in s["suggestion"]
               for s in suggestions)
    out2 = aggregate([_conv(19.0, [{"channel": "email"}])], None)
    assert any("unmeasured" in s["suggestion"]
               for s in suggest(out2))
    assert len(suggest(out)) <= 3


def test_sessions_from_snapshot_maps_sources():
    snap = {"top_sources": [
        {"name": "Google", "visitors": 120},
        {"name": "reddit", "visitors": 30},
        {"name": "Direct", "visitors": 10},
        {"name": None, "visitors": 5},
    ]}
    assert sessions_from_snapshot(snap) == {
        "seo": 120, "community": 30, "direct": 10}
    assert sessions_from_snapshot({}) is None
    assert sessions_from_snapshot(None) is None


def test_build_attribution_prefers_recorded_snapshot():
    state = {
        "metrics": [{"at": NOW.isoformat(), "source": "plausible",
                     "top_sources": [{"name": "Google", "visitors": 100},
                                     {"name": "reddit", "visitors": 50}]}],
        "conversions": [_conv(19.0, [{"channel": "community"}])],
    }
    out = build_attribution(state, now=NOW)
    assert out["sessions_source"] == "recorded snapshot"
    assert out["model"] == "last_touch"
    by_channel = {r["channel"]: r for r in out["channels"]}
    assert by_channel["community"]["conversion_rate"] == pytest.approx(0.02)
    assert out["conversions_count"] == 1


# -- CLI --------------------------------------------------------------------------

def _state(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, {"last_run": {}, "last_attempt": {}, "metrics": [],
                      "experiments": {}, "strategy_history": [],
                      "spend_usd": 0.0})
    return path


def test_cli_record_conversion_books_revenue_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    _state(tmp_path)
    code = growth.main(["record-conversion", "--amount", "19",
                        "--touches", "seo:google, community:r/cissp",
                        "--note", "first paying learner"])
    assert code == 0
    out = capsys.readouterr().out
    assert "recorded conversion" in out
    assert "seo → community" in out
    state = load_state(tmp_path / "state.json")
    assert len(state["conversions"]) == 1
    conv = state["conversions"][0]
    assert conv["amount_usd"] == 19.0
    assert conv["channels"] == ["seo", "community"]
    # The same function against one runtime books the revenue exactly
    # once (the CLI's runtime is separate, so check it directly).
    from core.attribution import parse_touches
    from ecosystem.runtime import build_runtime
    rt = build_runtime(approvals_persist=True)
    biz_id = growth._business_id(rt)
    fresh = {"conversions": []}
    growth.record_conversion(rt, biz_id, fresh, 19.0,
                             parse_touches("seo:google, community:r/cissp"))
    assert rt.ledger.pnl(biz_id).revenue == pytest.approx(19.0)
    assert len(fresh["conversions"]) == 1


def test_cli_record_revenue_with_channel_records_conversion(tmp_path, monkeypatch):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    _state(tmp_path)
    assert growth.main(["record-revenue", "--amount", "190",
                        "--channel", "community", "--source", "r/cissp"]) == 0
    state = load_state(tmp_path / "state.json")
    assert len(state["conversions"]) == 1
    assert state["conversions"][0]["channels"] == ["community"]
    # Without a channel it stays plain revenue, no conversion.
    assert growth.main(["record-revenue", "--amount", "19"]) == 0
    assert len(load_state(tmp_path / "state.json")["conversions"]) == 1


def test_cli_record_conversion_rejects_bad_channel(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    _state(tmp_path)
    assert growth.main(["record-conversion", "--amount", "19",
                        "--touches", "telepathy"]) == 1
    assert "unknown channel" in capsys.readouterr().out
    assert load_state(tmp_path / "state.json").get("conversions", []) == []


def test_cli_attribution_report_without_plausible(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.delenv("PLAUSIBLE_API_KEY", raising=False)
    path = _state(tmp_path)
    state = load_state(path)
    state["metrics"] = [{"at": NOW.isoformat(), "source": "plausible",
                         "top_sources": [{"name": "Google", "visitors": 100},
                                         {"name": "reddit", "visitors": 50}]}]
    state["conversions"] = [_conv(19.0, [{"channel": "community"}]),
                            _conv(19.0, [{"channel": "seo"}])]
    save_state(path, state)
    assert growth.main(["attribution"]) == 0
    reports = list((tmp_path / "reports").glob("attribution_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "Model: **last_touch**" in text
    assert "recorded snapshot" in text
    assert "| community | 50" in text
    assert "## Next moves" in text
    saved = load_state(path)
    assert saved["attribution_last"]["totals"]["revenue"] == 38.0
    out = capsys.readouterr().out
    assert "2 conversions" in out


def test_cli_attribution_linear_model(tmp_path, monkeypatch):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.delenv("PLAUSIBLE_API_KEY", raising=False)
    path = _state(tmp_path)
    state = load_state(path)
    state["conversions"] = [_conv(19.0, [{"channel": "seo"},
                                          {"channel": "community"}])]
    save_state(path, state)
    assert growth.main(["attribution", "--model", "linear"]) == 0
    saved = load_state(path)
    assert saved["attribution_last"]["model"] == "linear"


# -- dashboard ----------------------------------------------------------------------

def test_api_attribution(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from core.ledger import Ledger
    from core.registry import BusinessRegistry
    from dashboard.app import create_app
    from orchestrator.approvals import ApprovalGate
    from orchestrator.engine import Orchestrator
    from orchestrator.registry import AgentRegistry

    path = _state(tmp_path)
    state = load_state(path)
    state["metrics"] = [{"at": NOW.isoformat(), "source": "plausible",
                         "top_sources": [{"name": "Google", "visitors": 100}]}]
    state["conversions"] = [_conv(19.0, [{"channel": "seo"}])]
    save_state(path, state)
    monkeypatch.setenv("DAILY_PLAN_STATE_PATH", str(path))
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     approvals=ApprovalGate())
    r = TestClient(app).get("/api/attribution")
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "last_touch"
    assert body["totals"]["revenue"] == 19.0
    assert body["channels"][0]["channel"] == "seo"
    assert body["channels"][0]["conversion_rate"] == pytest.approx(0.01)
