"""Order fulfilment: Shopify order -> CJ order (cost-checked, approval
when over the approved cost) -> tracking back to Shopify; live
connection checks."""

import json
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.cj_orders import CJOrders, OrderQuote, SupplierOrderStatus
from core.shopify_adapter import ShopifyCommerceAdapter, ShopifyError
from ecosystem.fulfillment import (COST_ACTION, FulfilmentPolicy,
                                   fulfilment_overview, process_orders)
from ecosystem.shopify_pipeline import load_state, run_check
from orchestrator.approvals import ApprovalGate

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
PID = "gid://shopify/Product/1"
OID = "gid://shopify/Order/5001"


def _order(oid=OID, name="#1001", minutes_ago=120, pid=PID, qty=1, address=True):
    return {
        "id": oid, "name": name, "email": "buyer@example.com", "phone": None,
        "createdAt": (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z"),
        "cancelledAt": None,
        "shippingAddress": ({"name": "Pat Doe", "address1": "1 Main St", "address2": "",
                             "city": "Austin", "province": "Texas", "provinceCode": "TX",
                             "zip": "73301", "country": "United States",
                             "countryCodeV2": "US", "phone": "555"} if address else None),
        "lineItems": {"nodes": [{"id": "li1", "sku": "S", "quantity": qty,
                                 "product": {"id": pid}}]},
    }


class FakeShop:
    def __init__(self, orders):
        self.orders = orders
        self.fulfilled = []
        self.already_fulfilled = False

    def orders_to_fulfil(self):
        return [o for o in self.orders if o["id"] not in {f[0] for f in self.fulfilled}]

    def fulfil_with_tracking(self, oid, number, company=None, url=None):
        if self.already_fulfilled:
            raise ShopifyError(f"order {oid} has no open fulfillment orders")
        self.fulfilled.append((oid, number, company, url))
        return {"id": "f1"}


class FakeCJ:
    def __init__(self, total_product=6.5, shipping=4.2):
        self.product, self.shipping = total_product, shipping
        self.created = []
        self.paid = []
        self.tracking: dict[str, str] = {}
        self.fail_create = 0
        self.fail_pay = 0

    def quote(self, items, country, preferred_method=""):
        qty = sum(i["quantity"] for i in items)
        return OrderQuote(self.product * qty, self.shipping, preferred_method or "CJPacket", 12)

    def create_order(self, number, address, items, method):
        if self.fail_create:
            self.fail_create -= 1
            raise RuntimeError("CJ timeout")
        self.created.append({"number": number, "address": address, "items": items,
                             "method": method})
        return f"CJ{len(self.created)}"

    def pay_from_balance(self, cj_id):
        if self.fail_pay:
            self.fail_pay -= 1
            raise RuntimeError("insufficient balance")
        self.paid.append(cj_id)

    def order_status(self, cj_id):
        return SupplierOrderStatus("SHIPPED" if cj_id in self.tracking else "UNSHIPPED",
                                   self.tracking.get(cj_id), "CJPacket", 10.7)


@pytest.fixture
def gate(tmp_path):
    return ApprovalGate(persist_path=tmp_path / "approvals.json")


def _state(tmp_path, platform="cjdropshipping", landed=10.7):
    state = load_state(tmp_path / "state.json")
    state["products"][PID] = {
        "title": "Laptop Stand", "landed_cost_usd": landed, "price_usd": 29.99,
        "supplier": {"name": "CJ: Stand", "platform": platform, "variant_id": "V1",
                     "shipping_method": "CJPacket"}}
    return state


def test_order_placed_within_cost_after_hold_and_tracked(tmp_path, gate):
    state = _state(tmp_path)
    shop, cj = FakeShop([_order(), _order("gid://shopify/Order/5002", "#1002", minutes_ago=10)]), FakeCJ()
    res = process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    assert [r["status"] for r in res] == ["placed"]  # #1002 still in its hold period
    (created,) = cj.created
    assert created["number"] == "1001-5001"
    assert created["items"] == [{"vid": "V1", "quantity": 1}]
    assert created["address"]["country_code"] == "US" and created["method"] == "CJPacket"
    assert cj.paid == []  # auto-pay off by default
    # Second pass: no duplicate order, no tracking yet.
    assert process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW) == []
    assert len(cj.created) == 1
    cj.tracking["CJ1"] = "YT123"
    (r,) = process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    assert r == {"order": "#1001", "status": "fulfilled", "tracking_number": "YT123"}
    oid, number, company, url = shop.fulfilled[0]
    assert (oid, number, company) == (OID, "YT123", "CJPacket") and "YT123" in url


def test_addresses_never_persisted(tmp_path, gate):
    state = _state(tmp_path)
    process_orders(state, FakeShop([_order()]), FakeCJ(), gate, FulfilmentPolicy(), NOW)
    dumped = json.dumps(state) + json.dumps([a.details for a in gate.all()])
    assert "1 Main St" not in dumped and "Pat Doe" not in dumped and "buyer@" not in dumped


def test_over_cost_needs_approval_then_places(tmp_path, gate):
    state = _state(tmp_path)
    shop, cj = FakeShop([_order()]), FakeCJ(total_product=9.0)  # 13.20 > 10.70 * 1.1
    (r,) = process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    assert r["status"] == "awaiting_approval" and cj.created == []
    (appr,) = gate.pending()
    assert appr.action == COST_ACTION and appr.amount_usd == 13.2
    assert appr.details["approved_cost_usd"] == 10.7
    # Still pending -> nothing ordered, no duplicate approval.
    assert process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW) == []
    gate.decide(appr.id, True, "owner")
    res = process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    assert [r["status"] for r in res] == ["approved_over_cost", "placed"]
    assert len(cj.created) == 1 and len(gate.all()) == 1


def test_cost_rising_past_the_approved_amount_asks_again(tmp_path, gate):
    state = _state(tmp_path)
    shop, cj = FakeShop([_order()]), FakeCJ(total_product=9.0)
    process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    gate.decide(gate.pending()[0].id, True, "owner")
    cj.product = 12.0
    res = process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    assert res[-1]["status"] == "awaiting_approval" and cj.created == []


def test_rejected_over_cost_goes_manual(tmp_path, gate):
    state = _state(tmp_path)
    shop, cj = FakeShop([_order()]), FakeCJ(total_product=9.0)
    process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    gate.decide(gate.pending()[0].id, False, "owner")
    process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    assert state["orders"][OID]["status"] == "manual" and cj.created == []
    assert fulfilment_overview(state)["attention"][0]["status"] == "manual"


@pytest.mark.parametrize("setup,needle", [
    (lambda st, o: st["products"][PID]["supplier"].update(platform="alibaba"), "no CJ supplier"),
    (lambda st, o: o["lineItems"]["nodes"][0].update(product={"id": "gid://shopify/Product/9"}),
     "no CJ supplier"),
    (lambda st, o: o.update(shippingAddress=None), "protected customer data"),
])
def test_unautomatable_orders_go_manual(tmp_path, gate, setup, needle):
    state, order = _state(tmp_path), _order()
    setup(state, order)
    cj = FakeCJ()
    (r,) = process_orders(state, FakeShop([order]), cj, gate, FulfilmentPolicy(), NOW)
    assert r["status"] == "manual" and needle in r["reason"] and cj.created == []


def test_no_cj_configured_goes_manual(tmp_path, gate):
    (r,) = process_orders(_state(tmp_path), FakeShop([_order()]), None, gate,
                          FulfilmentPolicy(), NOW)
    assert "CJ not configured" in r["reason"]


def test_create_failure_retried_without_double_order(tmp_path, gate):
    state = _state(tmp_path)
    shop, cj = FakeShop([_order()]), FakeCJ()
    cj.fail_create = 1
    (r,) = process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    assert r["status"] == "error"
    (r,) = process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    assert r["status"] == "placed" and len(cj.created) == 1


def test_auto_pay_and_retry_on_low_balance(tmp_path, gate):
    state = _state(tmp_path)
    shop, cj = FakeShop([_order()]), FakeCJ()
    cj.fail_pay = 1
    saves = []
    pol = FulfilmentPolicy(auto_pay=True)
    process_orders(state, shop, cj, gate, pol, NOW, save=lambda: saves.append(1))
    rec = state["orders"][OID]
    assert rec["status"] == "placed" and not rec["paid"] and "balance" in rec["pay_error"]
    assert saves  # persisted right after the supplier-side change
    process_orders(state, shop, cj, gate, pol, NOW)
    assert rec["paid"] and cj.paid == ["CJ1"] and "pay_error" not in rec


def test_stuck_without_tracking_flagged_once(tmp_path, gate):
    state = _state(tmp_path)
    shop, cj = FakeShop([_order()]), FakeCJ()
    process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    later = NOW + timedelta(days=6)
    (r,) = process_orders(state, shop, cj, gate, FulfilmentPolicy(), later)
    assert r["status"] == "stuck" and r["days"] == 6
    assert process_orders(state, shop, cj, gate, FulfilmentPolicy(), later) == []
    assert fulfilment_overview(state)["attention"][0]["why"] == "no tracking yet"


def test_order_fulfilled_by_hand_in_shopify(tmp_path, gate):
    state = _state(tmp_path)
    shop, cj = FakeShop([_order()]), FakeCJ()
    process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    cj.tracking["CJ1"] = "YT1"
    shop.already_fulfilled = True
    (r,) = process_orders(state, shop, cj, gate, FulfilmentPolicy(), NOW)
    assert r["status"] == "fulfilled_elsewhere"


# -- adapters ---------------------------------------------------------------------

class GQL:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, method, url, headers, body):
        payload = json.loads(body)
        self.calls.append(payload)
        for needle, data in self.answers:
            if needle in payload["query"]:
                return 200, json.dumps({"data": data})
        raise AssertionError(payload["query"][:80])


def _shopify(answers):
    t = GQL(answers)
    return ShopifyCommerceAdapter("demo.myshopify.com", access_token="t", transport=t,
                                  sleep=lambda s: None), t


def test_shopify_orders_to_fulfil_filters_and_skips_cancelled():
    a, t = _shopify([("orders(", {"orders": {"nodes": [
        {"id": "o1", "cancelledAt": None}, {"id": "o2", "cancelledAt": "2026-10-05"}],
        "pageInfo": {"hasNextPage": False, "endCursor": None}}})])
    assert [o["id"] for o in a.orders_to_fulfil()] == ["o1"]
    q = t.calls[0]["variables"]["q"]
    assert "financial_status:paid" in q and "fulfillment_status:unfulfilled" in q


def test_shopify_fulfil_with_tracking_uses_open_fulfillment_orders():
    a, t = _shopify([
        ("fulfillmentOrders", {"order": {"fulfillmentOrders": {"nodes": [
            {"id": "fo1", "status": "OPEN"}, {"id": "fo2", "status": "CLOSED"}]}}}),
        ("fulfillmentCreate", {"fulfillmentCreate": {"fulfillment": {"id": "f", "status": "SUCCESS"},
                                                     "userErrors": []}})])
    a.fulfil_with_tracking("5001", "YT1", company="CJPacket", url="https://t/YT1")
    f = t.calls[1]["variables"]["fulfillment"]
    assert t.calls[0]["variables"]["id"] == "gid://shopify/Order/5001"
    assert f["lineItemsByFulfillmentOrder"] == [{"fulfillmentOrderId": "fo1"}]
    assert f["trackingInfo"] == {"number": "YT1", "company": "CJPacket", "url": "https://t/YT1"}
    assert f["notifyCustomer"] is True


def test_shopify_verify_reports_missing_scopes():
    from core.shopify_adapter import SCOPES

    granted = [s for s in SCOPES if s.startswith("write_") or s == "read_orders"]
    granted.remove("write_content")
    a, _ = _shopify([("currentAppInstallation", {
        "shop": {"name": "Demo", "myshopifyDomain": "demo.myshopify.com", "currencyCode": "USD"},
        "currentAppInstallation": {"accessScopes": [{"handle": h} for h in granted]}})])
    assert a.verify()["missing_scopes"] == ["read_locations", "read_content", "write_content"]


def test_cj_orders_quote_create_status():
    calls = []

    def transport(method, url, headers, body):
        path = urllib.parse.urlparse(url).path.split("/v1", 1)[1]
        calls.append((path, url, json.loads(body) if body else None))
        ok = lambda d: (200, json.dumps({"code": 200, "result": True, "data": d}))
        if path == "/authentication/getAccessToken":
            return ok({"accessToken": "tok"})
        if path == "/product/variant/queryByVid":
            return ok({"variantSellPrice": "6.50"})
        if path == "/logistic/freightCalculate":
            return ok([{"logisticName": "Cheap", "logisticPrice": 3.0, "logisticAging": "15-25"},
                       {"logisticName": "CJPacket", "logisticPrice": 4.2, "logisticAging": "7-12"}])
        if path == "/shopping/order/createOrderV2":
            return ok({"orderId": "CJ900"})
        if path == "/shopping/order/getOrderDetail":
            return ok({"orderStatus": "SHIPPED", "trackNumber": "YT9",
                       "logisticName": "CJPacket", "orderAmount": "17.2"})
        raise AssertionError(path)

    cj = CJOrders("k", transport=transport, sleep=lambda s: None)
    q = cj.quote([{"vid": "V1", "quantity": 2}], "US", preferred_method="CJPacket")
    assert (q.product_cost_usd, q.shipping_cost_usd, q.shipping_method, q.total_usd) == \
        (13.0, 4.2, "CJPacket", 17.2)
    assert cj.quote([{"vid": "V1", "quantity": 1}], "US").shipping_method == "Cheap"
    addr = {"name": "Pat", "address1": "1 Main", "city": "Austin", "country": "United States",
            "country_code": "US", "zip": "73301"}
    assert cj.create_order("1001-5001", addr, [{"vid": "V1", "quantity": 2}], "CJPacket") == "CJ900"
    body = next(c[2] for c in calls if c[0] == "/shopping/order/createOrderV2")
    assert body["orderNumber"] == "1001-5001" and body["shippingCountryCode"] == "US"
    assert body["products"] == [{"vid": "V1", "quantity": 2}]
    st = cj.order_status("CJ900")
    assert st.tracking_number == "YT9" and st.amount_usd == 17.2


# -- live check + dashboard + report -------------------------------------------

def test_run_check_reports_each_integration(capsys, monkeypatch):
    monkeypatch.delenv("CJ_API_KEY", raising=False)

    class Shop:
        api_version = "2026-07"

        def verify(self):
            return {"shop": {"name": "Demo", "myshopifyDomain": "d.myshopify.com",
                             "currencyCode": "USD"}, "missing_scopes": ["read_orders"]}

    class Meta:
        def verify(self):
            return {"name": "Acct", "currency": "USD", "active": True, "account_status": 1}

    class Google:
        def verify(self):
            raise RuntimeError("developer token not approved")

    code = run_check(Shop(), {"meta": Meta(), "google": Google()})
    out = capsys.readouterr().out
    assert code == 2
    assert "[FAIL] Shopify" in out and "missing scopes: read_orders" in out
    assert "[--  ] CJ Dropshipping" in out
    assert "[OK  ] Meta Ads" in out
    assert "[FAIL] Google Ads: developer token not approved" in out
    assert "2 problem(s)" in out


def test_dashboard_renders_cost_approval(gate):
    from fastapi.testclient import TestClient

    from dashboard.app import create_app

    gate.request(COST_ACTION, amount_usd=13.2, details={
        "order_id": OID, "order_name": "#1001", "products": ["Laptop Stand"],
        "approved_cost_usd": 10.7, "current_cost_usd": 13.2, "shipping_method": "CJPacket"})
    r = TestClient(create_app(approvals=gate)).get("/approvals")
    assert "Supplier cost went up" in r.text and "$13.20" in r.text and "#1001" in r.text


def test_store_report_lists_orders_needing_attention(tmp_path, gate):
    from ecosystem.store_ops import write_store_report

    state = _state(tmp_path)
    process_orders(state, FakeShop([_order()]), None, gate, FulfilmentPolicy(), NOW)
    path = tmp_path / "r.md"
    write_store_report(path, NOW, {}, state, [], [], [], [], 0.0)
    text = path.read_text(encoding="utf-8")
    assert "## Orders" in text and "Needs you" in text and "#1001" in text
