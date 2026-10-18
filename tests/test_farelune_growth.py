"""Tests for the Farelune growth program: one real business, ledger
wiring for agent costs, affiliate revenue and real-world ad spend."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ecosystem import farelune_growth as fg
from ecosystem.runtime import build_runtime


def test_business_is_created_once():
    rt = build_runtime()
    b1 = fg.get_or_create_business(rt)
    b2 = fg.get_or_create_business(rt)
    assert b1.id == b2.id
    assert len([b for b in rt.businesses.list()
                if b.name == "Farelune"]) == 1


def test_revenue_and_ad_spend_hit_the_same_books():
    rt = build_runtime()
    biz = fg.get_or_create_business(rt)
    fg.record_revenue(rt, biz.id, 23.10, "2026-10")
    fg.record_external_cost(rt, biz.id, 36.90,
                            "Meta campaign Oct 13-27 (USD equiv)")
    fg.post_step_cost(rt, biz.id, SimpleNamespace(cost_usd=0.42),
                      "seo_article 'cheap flights'")
    pnl = rt.ledger.pnl(biz.id)
    assert pnl.revenue == pytest.approx(23.10)
    assert pnl.total_costs == pytest.approx(36.90 + 0.42)
    assert pnl.net_profit == pytest.approx(23.10 - 37.32)
    kinds = {e.kind for e in rt.ledger.entries(biz.id)}
    assert kinds == {"revenue", "external_cost", "ai_spend"}


def test_booking_rejects_bad_input():
    rt = build_runtime()
    biz = fg.get_or_create_business(rt)
    with pytest.raises(ValueError):
        fg.record_revenue(rt, biz.id, 0, "2026-10")
    with pytest.raises(ValueError):
        fg.record_revenue(rt, biz.id, 10, "")
    with pytest.raises(ValueError):
        fg.record_external_cost(rt, biz.id, -1, "Meta")
    with pytest.raises(ValueError):
        fg.record_external_cost(rt, biz.id, 10, "")


def test_record_cost_cli(capsys):
    rt = build_runtime()
    with patch.object(fg, "build_runtime", return_value=rt):
        rc = fg.main(["--record-cost", "36.90", "--label", "Meta flight"])
    assert rc == 0
    biz = fg.get_or_create_business(rt)
    assert rt.ledger.pnl(biz.id).total_costs == pytest.approx(36.90)


def test_growth_run_refuses_when_site_not_live(capsys):
    rt = build_runtime()
    with patch.object(fg, "build_runtime", return_value=rt), \
            patch.object(fg, "_require_llm_ready", return_value=None), \
            patch.object(fg, "site_is_live", return_value=False):
        rc = fg.main(["--no-llm"])
    assert rc == 2
    biz = fg.get_or_create_business(rt)
    assert rt.ledger.entries(biz.id) == []
