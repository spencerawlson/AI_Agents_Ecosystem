"""Tests for the ProofFlow growth program: one real business, real ledger
wiring for costs and owner-reported AdSense revenue, and the not-live
gate that refuses to run against a site that isn't serving."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ecosystem import proofflow_growth as pg
from ecosystem.runtime import build_runtime


def test_business_is_created_once():
    rt = build_runtime()
    b1 = pg.get_or_create_business(rt)
    b2 = pg.get_or_create_business(rt)
    assert b1.id == b2.id
    assert len([b for b in rt.businesses.list()
                if b.name == "ProofFlow"]) == 1


def test_record_adsense_revenue_posts_real_entry():
    rt = build_runtime()
    biz = pg.get_or_create_business(rt)
    entry = pg.record_adsense_revenue(rt, biz.id, 18.42, "2026-10")
    assert entry.amount_usd == 18.42
    assert "AdSense earnings 2026-10" in entry.description
    assert "owner-reported" in entry.description
    pnl = rt.ledger.pnl(biz.id)
    assert pnl.revenue == pytest.approx(18.42)
    assert pnl.net_profit == pytest.approx(18.42)


def test_record_adsense_revenue_rejects_bad_input():
    rt = build_runtime()
    biz = pg.get_or_create_business(rt)
    with pytest.raises(ValueError):
        pg.record_adsense_revenue(rt, biz.id, 0, "2026-10")
    with pytest.raises(ValueError):
        pg.record_adsense_revenue(rt, biz.id, -5, "2026-10")
    with pytest.raises(ValueError):
        pg.record_adsense_revenue(rt, biz.id, 10, "")


def test_step_cost_posts_negative_ai_spend():
    rt = build_runtime()
    biz = pg.get_or_create_business(rt)
    pg.post_step_cost(rt, biz.id, SimpleNamespace(cost_usd=0.35),
                      "seo_article 'proof of delivery'")
    pg.post_step_cost(rt, biz.id, SimpleNamespace(cost_usd=0.0), "free step")
    entries = rt.ledger.entries(biz.id)
    assert len(entries) == 1
    assert entries[0].amount_usd == pytest.approx(-0.35)
    assert entries[0].kind == "ai_spend"


def test_record_revenue_cli_updates_the_books(capsys):
    rt = build_runtime()
    with patch.object(pg, "build_runtime", return_value=rt):
        rc = pg.main(["--record-revenue", "7.25", "--period", "2026-11"])
    assert rc == 0
    biz = pg.get_or_create_business(rt)
    assert rt.ledger.pnl(biz.id).revenue == pytest.approx(7.25)


def test_growth_run_refuses_when_site_not_live(capsys):
    rt = build_runtime()
    with patch.object(pg, "build_runtime", return_value=rt), \
            patch.object(pg, "_require_llm_ready", return_value=None), \
            patch.object(pg, "site_is_live", return_value=False):
        rc = pg.main(["--no-llm"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "not serving the new site" in out
    biz = pg.get_or_create_business(rt)
    assert rt.ledger.entries(biz.id) == []
