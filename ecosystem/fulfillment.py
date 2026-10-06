"""Order fulfilment: paid Shopify order -> CJ supplier order -> tracking
back to Shopify (which emails the customer).

Each cycle:
  1. Paid, unfulfilled orders older than the hold period (default 60 min,
     time for cancellations and Shopify's fraud analysis) are matched to
     the approved supplier of each product.
  2. The supplier cost is re-quoted live. Within the approved landed cost
     (+10% tolerance) the CJ order is placed automatically; above it, a
     `supplier_order_over_cost` approval is opened and nothing is ordered
     until the owner decides (reject = owner handles it manually).
  3. Placed orders are polled for a tracking number; when one appears the
     Shopify order is marked fulfilled with tracking.

Orders that can't be automated (non-CJ supplier, product not from the
pipeline, no shipping address) are marked `manual` and listed in the
store report. Shipping addresses are never written to state, approvals
or reports — they're read from Shopify only when placing the order.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from core.cj_orders import TRACKING_URL
from orchestrator.approvals import ApprovalGate
from orchestrator.models import ApprovalStatus

log = logging.getLogger("ecosystem.fulfillment")

COST_ACTION = "supplier_order_over_cost"
CJ_PLATFORM = "cjdropshipping"
# Rec statuses that a new cycle should (re)try placing.
RETRYABLE = ("approved_over_cost", "error")


@dataclass
class FulfilmentPolicy:
    cost_tolerance: float = 0.10
    hold_minutes: int = 60
    auto_pay: bool = False
    stuck_tracking_days: int = 5

    @classmethod
    def from_env(cls) -> "FulfilmentPolicy":
        e = os.environ
        p = cls()
        if e.get("FULFILMENT_COST_TOLERANCE"):
            p.cost_tolerance = float(e["FULFILMENT_COST_TOLERANCE"])
        if e.get("FULFILMENT_HOLD_MINUTES"):
            p.hold_minutes = int(e["FULFILMENT_HOLD_MINUTES"])
        if e.get("FULFILMENT_STUCK_DAYS"):
            p.stuck_tracking_days = int(e["FULFILMENT_STUCK_DAYS"])
        p.auto_pay = e.get("CJ_AUTO_PAY", "").lower() in ("1", "true", "yes")
        return p


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _address(order: dict) -> dict | None:
    a = order.get("shippingAddress") or {}
    if not (a.get("address1") and a.get("countryCodeV2") and a.get("city")):
        return None
    return {"name": a.get("name") or "", "phone": a.get("phone") or order.get("phone") or "",
            "email": order.get("email") or "", "address1": a["address1"],
            "address2": a.get("address2") or "", "city": a["city"],
            "province": a.get("province") or a.get("provinceCode") or "",
            "zip": a.get("zip") or "", "country": a.get("country") or "",
            "country_code": a["countryCodeV2"]}


def _supplier_order_number(order: dict) -> str:
    # Unique per Shopify order so a retried create can't double-order.
    return f"{order['name'].lstrip('#')}-{order['id'].rsplit('/', 1)[-1]}"


def _apply_cost_decisions(state: dict, gate: ApprovalGate, results: list) -> None:
    for appr in gate.all():
        if appr.action != COST_ACTION or appr.status == ApprovalStatus.PENDING:
            continue
        rec = state["orders"].get(appr.details.get("order_id", ""))
        if not rec or rec.get("status") != "awaiting_approval" or rec.get("approval_id") != appr.id:
            continue
        if appr.status == ApprovalStatus.APPROVED:
            rec["status"] = "approved_over_cost"
            rec["approved_cost_usd"] = appr.amount_usd
        else:
            rec.update(status="manual", reason="owner rejected the higher supplier cost")
        results.append({"order": rec.get("order_name"), "status": rec["status"]})


def process_orders(state: dict, shopify, cj, gate: ApprovalGate,
                   policy: FulfilmentPolicy | None = None,
                   now: datetime | None = None,
                   save: Callable[[], None] = lambda: None) -> list[dict]:
    """One fulfilment pass. Mutates state; calls save() after every
    supplier-side change so a crash can never cause a double order."""
    policy = policy or FulfilmentPolicy()
    now = now or datetime.now(timezone.utc)
    results: list[dict] = []
    placed_now: set[str] = set()
    _apply_cost_decisions(state, gate, results)

    for order in shopify.orders_to_fulfil():
        oid = order["id"]
        rec = state["orders"].get(oid)
        if rec and rec.get("status") not in RETRYABLE:
            continue
        if _parse_ts(order["createdAt"]) + timedelta(minutes=policy.hold_minutes) > now:
            continue
        name = order.get("name", oid)
        rec = {**(rec or {}), "order_name": name, "created_at": order["createdAt"]}
        state["orders"][oid] = rec

        def manual(reason: str) -> None:
            rec.update(status="manual", reason=reason)
            results.append({"order": name, "status": "manual", "reason": reason})

        lines = (order.get("lineItems") or {}).get("nodes", [])
        items, approved_cost, titles, method = [], 0.0, [], ""
        unmatched = None
        for li in lines:
            pid = (li.get("product") or {}).get("id")
            prod = state["products"].get(pid)
            sup = (prod or {}).get("supplier") or {}
            if prod is None or sup.get("platform") != CJ_PLATFORM or not sup.get("variant_id"):
                unmatched = (prod or {}).get("title") or li.get("sku") or "unknown item"
                break
            qty = int(li.get("quantity") or 0)
            items.append({"vid": sup["variant_id"], "quantity": qty})
            approved_cost += float(prod["landed_cost_usd"]) * qty
            titles.append(prod["title"])
            method = method or sup.get("shipping_method", "")
        if unmatched:
            manual(f"'{unmatched}' has no CJ supplier on file — fulfil in Shopify")
            continue
        if not items:
            manual("order has no shippable line items")
            continue
        if cj is None:
            manual("CJ not configured (CJ_API_KEY) — fulfil in Shopify")
            continue
        address = _address(order)
        if address is None:
            manual("no shipping address readable — enable protected customer data "
                   "access for the Shopify app")
            continue

        try:
            quote = cj.quote(items, address["country_code"], preferred_method=method)
            ceiling = (rec.get("approved_cost_usd") if rec.get("status") == "approved_over_cost"
                       else approved_cost * (1 + policy.cost_tolerance))
            if quote.total_usd > float(ceiling) + 1e-9:
                appr = gate.request(
                    action=COST_ACTION, amount_usd=quote.total_usd, requested_by="fulfillment",
                    details={"order_id": oid, "order_name": name, "products": titles,
                             "approved_cost_usd": round(approved_cost, 2),
                             "current_cost_usd": quote.total_usd,
                             "shipping_method": quote.shipping_method})
                rec.update(status="awaiting_approval", approval_id=appr.id,
                           quoted_cost_usd=quote.total_usd)
                results.append({"order": name, "status": "awaiting_approval",
                                "approved_cost_usd": round(approved_cost, 2),
                                "current_cost_usd": quote.total_usd})
                save()
                continue
            cj_id = cj.create_order(_supplier_order_number(order), address, items,
                                    quote.shipping_method)
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            rec.update(status="error", error=str(exc)[:300])
            results.append({"order": name, "status": "error", "error": rec["error"]})
            save()
            continue
        rec.update(status="placed", cj_order_id=cj_id, placed_at=now.isoformat(),
                   supplier_cost_usd=quote.total_usd, paid=False)
        rec.pop("error", None)
        placed_now.add(oid)
        save()
        results.append({"order": name, "status": "placed", "cj_order_id": cj_id,
                        "supplier_cost_usd": quote.total_usd})
        if policy.auto_pay:
            _pay(cj, rec, save)

    for oid, rec in state["orders"].items():
        if rec.get("status") != "placed" or cj is None:
            continue
        if policy.auto_pay and not rec.get("paid") and oid not in placed_now:
            _pay(cj, rec, save)  # retry a failed wallet payment once per cycle
        try:
            st = cj.order_status(rec["cj_order_id"])
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"status: {exc}"[:300]
            continue
        if st.tracking_number:
            try:
                shopify.fulfil_with_tracking(
                    oid, st.tracking_number, company=st.carrier,
                    url=TRACKING_URL.format(number=st.tracking_number))
                rec.update(status="fulfilled")
            except Exception as exc:  # noqa: BLE001
                if "no open fulfillment orders" in str(exc):
                    rec.update(status="fulfilled_elsewhere")
                else:
                    rec["error"] = f"fulfil: {exc}"[:300]
                    continue
            rec.update(tracking_number=st.tracking_number, carrier=st.carrier,
                       fulfilled_at=now.isoformat())
            rec.pop("error", None)
            save()
            results.append({"order": rec.get("order_name"), "status": rec["status"],
                            "tracking_number": st.tracking_number})
        else:
            age = now - _parse_ts(rec["placed_at"])
            stuck = age >= timedelta(days=policy.stuck_tracking_days)
            if stuck and not rec.get("stuck"):
                results.append({"order": rec.get("order_name"), "status": "stuck",
                                "days": age.days})
            rec["stuck"] = stuck
    return results


def _pay(cj, rec: dict, save: Callable[[], None]) -> None:
    try:
        cj.pay_from_balance(rec["cj_order_id"])
        rec["paid"] = True
        rec.pop("pay_error", None)
    except Exception as exc:  # noqa: BLE001 - e.g. wallet balance too low
        rec["pay_error"] = str(exc)[:300]
    save()


def fulfilment_overview(state: dict) -> dict:
    counts: dict[str, int] = {}
    attention = []
    for rec in state["orders"].values():
        st = rec.get("status", "?")
        counts[st] = counts.get(st, 0) + 1
        if st in ("manual", "awaiting_approval", "error") or rec.get("stuck") or rec.get("pay_error"):
            why = (rec.get("reason") or rec.get("error") or rec.get("pay_error")
                   or ("no tracking yet" if rec.get("stuck") else st))
            attention.append({"order": rec.get("order_name"), "status": st, "why": why})
    return {"counts": counts, "attention": attention}
