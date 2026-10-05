"""Tests for the Experiment 001 Etsy monitor.

The stop-loss logic is tested with stubbed Etsy API responses (no network).
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ecosystem import etsy_monitor
from ecosystem.etsy_monitor import EtsyMonitor


def _receipt(ts: int, cents: int) -> dict:
    return {"receipt_id": 1, "created_timestamp": ts,
            "grandtotal": {"value": cents, "currency_code": "USD"}}


def _monitor(monkeypatch, listings_n=12, receipts=None, spend=1.76,
             today=None):
    mon = EtsyMonitor()
    monkeypatch.setattr(mon, "active_listings",
                        lambda: [{"listing_id": i} for i in range(listings_n)])
    monkeypatch.setattr(mon, "receipts", lambda limit=100: receipts or [])
    monkeypatch.setattr(EtsyMonitor, "cumulative_spend_usd",
                        staticmethod(lambda: spend))
    if today is not None:
        class FakeDate(date):
            @classmethod
            def today(cls):
                return today
        monkeypatch.setattr(etsy_monitor, "date", FakeDate)
    return mon


def test_fresh_launch_is_on_track(monkeypatch):
    mon = _monitor(monkeypatch, today=etsy_monitor.LAUNCH_DATE)
    s = mon.check()
    assert s["verdict"] == "ON_TRACK"
    assert s["stop_loss_triggers"] == []
    assert s["days_elapsed"] == 0
    assert s["days_remaining"] == 60


def test_budget_exhausted_triggers_stop_loss(monkeypatch):
    mon = _monitor(monkeypatch, spend=91.0)
    s = mon.check()
    assert s["verdict"] == "STOP_LOSS"
    assert any("budget exhausted" in t for t in s["stop_loss_triggers"])


def test_day30_no_growth_triggers_stop_loss(monkeypatch):
    mon = _monitor(monkeypatch, today=date(2026, 11, 2), spend=10.0)
    s = mon.check()
    assert s["days_elapsed"] == 30
    assert s["verdict"] == "STOP_LOSS"
    assert any("no week-over-week growth" in t for t in s["stop_loss_triggers"])


def test_revenue_counts_and_pace(monkeypatch):
    import time
    now = int(time.time())
    receipts = [_receipt(now - 86400 * i, 1200) for i in range(5)]  # 5 x $12
    mon = _monitor(monkeypatch, receipts=receipts, spend=5.0)
    s = mon.check()
    assert s["orders"] == 5
    assert s["revenue_usd"] == 60.0
    assert s["contribution_margin_usd"] == 55.0


def test_negative_margin_after_20_orders_triggers_stop_loss(monkeypatch):
    import time
    now = int(time.time())
    receipts = [_receipt(now - 86400 * i, 100) for i in range(20)]  # 20 x $1
    mon = _monitor(monkeypatch, receipts=receipts, spend=50.0)
    s = mon.check()
    assert s["verdict"] == "STOP_LOSS"
    assert any("contribution margin" in t for t in s["stop_loss_triggers"])


def test_missing_credentials_raise_cleanly(monkeypatch, tmp_path):
    monkeypatch.setattr(etsy_monitor, "CONFIG_DIR", tmp_path / "empty")
    for var in ("ETSY_KEYSTRING", "ETSY_SHARED_SECRET", "ETSY_ACCESS_TOKEN",
                "ETSY_REFRESH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(etsy_monitor.EtsyCredentialsError):
        etsy_monitor.load_credentials()
