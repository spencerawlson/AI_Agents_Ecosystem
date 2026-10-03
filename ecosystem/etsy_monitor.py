"""Experiment 001 (Evergreen Planners / Etsy) monitoring.

Pulls live shop state from the Etsy API and evaluates it against the
experiment charter: $1,000 revenue in 60 days, 84+ orders, stop-loss
triggers. Designed to run inside the worker loop and to survive
unattended operation (OAuth tokens auto-refresh on 401).

Credentials resolve in this order:
  1. ETSY_KEYSTRING / ETSY_SHARED_SECRET / ETSY_ACCESS_TOKEN /
     ETSY_REFRESH_TOKEN / ETSY_SHOP_ID environment variables
  2. ~/.config/evergreen-etsy/{etsy_config.json,tokens.json} (0600)

Spend tracking: Etsy Ads spend is enabled manually in the dashboard and
is not visible via the API, so cumulative spend lives in
~/.config/evergreen-etsy/spend.json (or ETSY_SPEND_USD). Update it when
spend happens; the stop-loss math reads it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

CONFIG_DIR = Path(os.environ.get(
    "EVERGREEN_ETSY_CONFIG_DIR",
    str(Path.home() / ".config" / "evergreen-etsy"),
))

# --- Experiment 001 charter constants ---------------------------------------
LAUNCH_DATE = date(2026, 10, 3)
DURATION_DAYS = 60
TARGET_REVENUE_USD = 1000.0
TARGET_ORDERS = 84
TOTAL_BUDGET_USD = 91.0
# Listing fees actually paid at launch (12 listings, ~CA$2.40).
BASELINE_SPEND_USD = 1.76
EXPERIMENT_BUSINESS_NAME = "Evergreen Planners"
EXPERIMENT_BUSINESS_TYPE = "etsy_digital_downloads"


class EtsyCredentialsError(RuntimeError):
    pass


def load_credentials() -> dict:
    """Return {keystring, shared_secret, access_token, refresh_token, shop_id}."""
    env = {
        "keystring": os.environ.get("ETSY_KEYSTRING"),
        "shared_secret": os.environ.get("ETSY_SHARED_SECRET"),
        "access_token": os.environ.get("ETSY_ACCESS_TOKEN"),
        "refresh_token": os.environ.get("ETSY_REFRESH_TOKEN"),
        "shop_id": os.environ.get("ETSY_SHOP_ID", "65154169"),
    }
    if all(env[k] for k in ("keystring", "shared_secret", "access_token",
                            "refresh_token")):
        return env
    cfg_path = CONFIG_DIR / "etsy_config.json"
    tok_path = CONFIG_DIR / "tokens.json"
    if not (cfg_path.exists() and tok_path.exists()):
        raise EtsyCredentialsError(
            "no Etsy credentials: set ETSY_* env vars or populate "
            f"{CONFIG_DIR}/{{etsy_config.json,tokens.json}}"
        )
    cfg = json.loads(cfg_path.read_text())
    tok = json.loads(tok_path.read_text())
    return {
        "keystring": cfg["keystring"],
        "shared_secret": cfg["shared_secret"],
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "shop_id": os.environ.get("ETSY_SHOP_ID", "65154169"),
    }


def _persist_tokens(access_token: str, refresh_token: str) -> None:
    tok_path = CONFIG_DIR / "tokens.json"
    if not tok_path.exists():
        return  # env-var mode: nothing to persist to
    tok = json.loads(tok_path.read_text())
    tok["access_token"] = access_token
    tok["refresh_token"] = refresh_token
    tok_path.write_text(json.dumps(tok))
    tok_path.chmod(0o600)


def _make_adapter(creds: dict):
    from core.etsy_adapter import EtsyCommerceAdapter

    return EtsyCommerceAdapter(
        creds["keystring"], creds["shared_secret"],
        creds["access_token"], creds["shop_id"],
    )


def _refresh(creds: dict) -> dict:
    from core.etsy_adapter import EtsyAuth

    new = EtsyAuth.refresh(creds["keystring"], creds["refresh_token"])
    creds = dict(creds)
    creds["access_token"] = new["access_token"]
    creds["refresh_token"] = new.get("refresh_token", creds["refresh_token"])
    _persist_tokens(creds["access_token"], creds["refresh_token"])
    return creds


class EtsyMonitor:
    """Live Experiment 001 monitor with auto-refreshing Etsy auth."""

    def __init__(self) -> None:
        self._creds: dict | None = None
        self._adapter = None

    def _ensure(self):
        if self._adapter is None:
            self._creds = load_credentials()
            self._adapter = _make_adapter(self._creds)
        return self._adapter

    def _call(self, method_name: str, *args, **kwargs):
        """Call an adapter method by name, refreshing OAuth once on 401."""
        adapter = self._ensure()
        try:
            return getattr(adapter, method_name)(*args, **kwargs)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
        self._creds = _refresh(self._creds)
        self._adapter = _make_adapter(self._creds)
        return getattr(self._adapter, method_name)(*args, **kwargs)

    # -- raw shop reads ------------------------------------------------------
    def active_listings(self) -> list[dict]:
        return self._call("list_products")

    def receipts(self, limit: int = 100) -> list[dict]:
        return self._call("list_orders", limit)

    def shop(self) -> dict:
        adapter = self._ensure()
        try:
            return adapter._request("GET", f"/shops/{self._creds['shop_id']}")
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
        self._creds = _refresh(self._creds)
        self._adapter = _make_adapter(self._creds)
        return self._adapter._request("GET", f"/shops/{self._creds['shop_id']}")

    # -- spend ----------------------------------------------------------------
    @staticmethod
    def cumulative_spend_usd() -> float:
        if os.environ.get("ETSY_SPEND_USD"):
            return float(os.environ["ETSY_SPEND_USD"])
        spend_path = CONFIG_DIR / "spend.json"
        if spend_path.exists():
            data = json.loads(spend_path.read_text())
            return float(data.get("cumulative_spend_usd", BASELINE_SPEND_USD))
        return BASELINE_SPEND_USD

    @staticmethod
    def record_spend(amount_usd: float, note: str = "") -> float:
        """Add to cumulative spend (ads, fees, tools). Returns new total."""
        spend_path = CONFIG_DIR / "spend.json"
        data = {"cumulative_spend_usd": BASELINE_SPEND_USD, "entries": []}
        if spend_path.exists():
            data = json.loads(spend_path.read_text())
        data["cumulative_spend_usd"] = round(
            float(data.get("cumulative_spend_usd", BASELINE_SPEND_USD))
            + amount_usd, 2)
        data.setdefault("entries", []).append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "amount_usd": amount_usd,
            "note": note,
        })
        spend_path.write_text(json.dumps(data, indent=1))
        return data["cumulative_spend_usd"]

    # -- experiment evaluation -------------------------------------------------
    def check(self) -> dict:
        """Full Experiment 001 status snapshot."""
        today = date.today()
        days_elapsed = max(0, (today - LAUNCH_DATE).days)
        days_remaining = max(0, DURATION_DAYS - days_elapsed)

        listings = self.active_listings()
        receipts = self.receipts()

        revenue = 0.0
        daily: dict[str, float] = {}
        for r in receipts:
            total = r.get("grandtotal") or {}
            amount = float(total.get("value", 0) or 0) / 100.0
            revenue += amount
            ts = r.get("created_timestamp") or 0
            day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            daily[day] = daily.get(day, 0.0) + amount
        orders = len(receipts)

        # Week-over-week revenue growth over the last 3 weeks.
        def week_revenue(weeks_ago: int) -> float:
            end = today.toordinal() - weeks_ago * 7
            start = end - 6
            return sum(v for d, v in daily.items()
                       if start <= date.fromisoformat(d).toordinal() <= end)

        wow = [week_revenue(w) for w in (0, 1, 2)]
        no_growth_2w = (
            days_elapsed >= 14
            and wow[0] <= wow[1] <= wow[2]
            and not any(wow) or (wow[0] == 0 and wow[1] == 0)
        )

        spend = self.cumulative_spend_usd()
        expected_pace = TARGET_REVENUE_USD * min(1.0, days_elapsed / DURATION_DAYS)
        contribution_margin = revenue - spend

        stop_loss = []
        if spend >= TOTAL_BUDGET_USD and revenue < 200:
            stop_loss.append(
                f"budget exhausted (${spend:.2f} >= ${TOTAL_BUDGET_USD}) "
                f"with revenue ${revenue:.2f} < $200")
        if days_elapsed >= 30 and revenue < 250 and no_growth_2w:
            stop_loss.append(
                f"day {days_elapsed}: revenue ${revenue:.2f} < $250 with "
                "no week-over-week growth for 2 weeks")
        if orders >= 20 and contribution_margin < 0:
            stop_loss.append(
                f"negative contribution margin (${contribution_margin:.2f}) "
                f"after {orders} orders")

        verdict = "STOP_LOSS" if stop_loss else (
            "ON_TRACK" if revenue >= expected_pace else "BEHIND")

        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "days_elapsed": days_elapsed,
            "days_remaining": days_remaining,
            "active_listings": len(listings),
            "orders": orders,
            "revenue_usd": round(revenue, 2),
            "spend_usd": round(spend, 2),
            "contribution_margin_usd": round(contribution_margin, 2),
            "target_revenue_usd": TARGET_REVENUE_USD,
            "target_orders": TARGET_ORDERS,
            "expected_pace_usd": round(expected_pace, 2),
            "pace_pct": round(100 * revenue / expected_pace, 1)
            if expected_pace > 0 else 100.0,
            "weekly_revenue_usd": [round(w, 2) for w in wow],
            "verdict": verdict,
            "stop_loss_triggers": stop_loss,
        }


def check_experiment_001() -> dict:
    """One-shot check. Raises EtsyCredentialsError when not configured."""
    return EtsyMonitor().check()
