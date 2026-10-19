"""Tests for the Notion templates business books: the program must
land on the business the discovery loop already created (same name),
and book channel revenue / real-world costs onto it."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ecosystem import notion_templates_growth as ng
from ecosystem.runtime import build_runtime


def test_business_matches_the_discovery_created_one():
    rt = build_runtime()
    existing = rt.businesses.create(ng.BUSINESS_NAME, "digital_product")
    biz = ng.get_or_create_business(rt)
    assert biz.id == existing.id
    assert len([b for b in rt.businesses.list()
                if b.name == ng.BUSINESS_NAME]) == 1


def test_revenue_and_costs_hit_the_books():
    rt = build_runtime()
    biz = ng.get_or_create_business(rt)
    ng.record_revenue(rt, biz.id, 29.0, "2026-10", source="Gumroad")
    ng.record_revenue(rt, biz.id, 19.0, "2026-10", source="Etsy")
    ng.record_external_cost(rt, biz.id, 0.20, "Etsy listing fee")
    pnl = rt.ledger.pnl(biz.id)
    assert pnl.revenue == pytest.approx(48.0)
    assert pnl.total_costs == pytest.approx(0.20)
    assert pnl.net_profit == pytest.approx(47.80)
    descs = [e.description for e in rt.ledger.entries(biz.id)
             if e.kind == "revenue"]
    assert any("Gumroad" in d for d in descs)
    assert any("Etsy" in d for d in descs)


def test_booking_rejects_bad_input():
    rt = build_runtime()
    biz = ng.get_or_create_business(rt)
    with pytest.raises(ValueError):
        ng.record_revenue(rt, biz.id, 0, "2026-10")
    with pytest.raises(ValueError):
        ng.record_revenue(rt, biz.id, 10, "")
    with pytest.raises(ValueError):
        ng.record_external_cost(rt, biz.id, -1, "fee")
    with pytest.raises(ValueError):
        ng.record_external_cost(rt, biz.id, 10, "")


def test_cli_pnl_and_record_revenue(capsys):
    rt = build_runtime()
    with patch.object(ng, "build_runtime", return_value=rt):
        assert ng.main(["--record-revenue", "29", "--period", "2026-10",
                        "--source", "Gumroad"]) == 0
        assert ng.main(["--pnl"]) == 0
    out = capsys.readouterr().out
    assert "booked revenue $29.00" in out
    assert "revenue $29.00" in out
