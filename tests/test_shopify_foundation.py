"""Shopify foundation: margin pricing, supplier scoring + report, the
Shopify GraphQL adapter (fake transport), and the brief -> approval ->
publish pipeline including idempotency and resumable failures."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.pricing import FeeSchedule, price_for_margin, quote
from core.shopify_adapter import (ShopifyCommerceAdapter,
                                  ShopifyCredentialsError, ShopifyError)
from core.sourcing import (ProductBrief, Supplier, alibaba_search_url,
                           rank_suppliers, render_supplier_report)
from ecosystem.shopify_pipeline import (APPROVAL_ACTION, ListingWriter,
                                        publish_approved, source)
from orchestrator.approvals import ApprovalGate

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "shopify_brief.example.json"


# -- pricing -------------------------------------------------------------------

def test_price_hits_target_margin_after_fees():
    q = price_for_margin(3.80, 4.50, target_margin=0.60)
    assert q.landed_cost_usd == 8.30
    assert q.margin >= 0.60
    assert f"{q.price_usd:.2f}".endswith(".99")
    # Exact formula: (8.30 + 0.30) / (1 - 0.60 - 0.029) = 23.18 -> 23.99
    assert q.price_usd == 23.99
    assert q.fees_usd == round(23.99 * 0.029 + 0.30, 2)
    assert q.profit_usd == round(23.99 - 8.30 - q.fees_usd, 2)


def test_price_without_charm_is_minimal():
    q = price_for_margin(10, target_margin=0.60, charm=False)
    assert 0.60 <= q.margin < 0.601


def test_max_ad_cost_keeps_min_net_margin():
    q = quote(30.0, 8.0, min_net_margin=0.20)
    net_after_ads = q.profit_usd - q.max_ad_cost_per_order_usd
    assert net_after_ads == pytest.approx(0.20 * 30.0, abs=0.01)


def test_impossible_margin_rejected():
    with pytest.raises(ValueError):
        price_for_margin(5, target_margin=0.98, fees=FeeSchedule(pct=0.03))
    with pytest.raises(ValueError):
        price_for_margin(-1)


# -- sourcing ------------------------------------------------------------------

def _brief(**kw) -> ProductBrief:
    return ProductBrief.model_validate_json(EXAMPLE.read_text()).model_copy(update=kw)


def test_reliable_supplier_ranks_first_and_risks_flagged():
    ranked = rank_suppliers(_brief())
    assert ranked[0].supplier.name.startswith("Example Supplier A")
    b_flags = " ".join(ranked[1].flags)
    assert "no trade assurance" in b_flags
    assert "MOQ 200" in b_flags
    assert "slow delivery" in b_flags


def test_overpriced_supplier_flagged_against_market():
    brief = _brief(market_price_usd=[9.99, 14.99])
    ranked = rank_suppliers(brief)
    assert all(any("above market high" in f for f in s.flags) for s in ranked)


def test_report_is_table_safe_and_lists_searches():
    brief = _brief()
    brief.suppliers[0].name = "Pipe | Co"
    md = render_supplier_report(brief, rank_suppliers(brief), "2026-10-05T00:00",
                                {"Pipe | Co": "appr_x"})
    assert "Pipe / Co" in md and "Pipe | Co" not in md
    assert "appr_x" in md
    assert alibaba_search_url("Adjustable Posture Corrector") in md
    assert "NOTHING WAS PUBLISHED" in md


# -- Shopify adapter (fake transport) -----------------------------------------

class FakeShopify:
    """Records GraphQL calls and answers them like the Admin API."""

    def __init__(self):
        self.calls: list[tuple[str, dict, dict]] = []
        self.fail_publish = 0
        self.throttle_once = False
        self.token_requests = 0
        self.reject_token: str | None = None

    def __call__(self, method, url, headers, body):
        payload = json.loads(body) if body else {}
        if url.endswith("/admin/oauth/access_token"):
            self.token_requests += 1
            return 200, json.dumps({"access_token": f"tok{self.token_requests}",
                                    "expires_in": 86399})
        if headers.get("X-Shopify-Access-Token") == self.reject_token:
            return 401, "unauthorized"
        q, v = payload["query"], payload.get("variables") or {}
        self.calls.append((q, v, headers))
        if self.throttle_once:
            self.throttle_once = False
            return 200, json.dumps({"errors": [
                {"message": "Throttled", "extensions": {"code": "THROTTLED"}}]})
        return 200, json.dumps({"data": self._answer(q, v)})

    def _product(self, title="T", status="DRAFT"):
        return {"id": "gid://shopify/Product/1", "title": title, "handle": "t",
                "status": status, "onlineStoreUrl": None,
                "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/9",
                                        "sku": None, "price": "0.00"}]}}

    def _answer(self, q, v):
        if "productCreate" in q:
            return {"productCreate": {"product": self._product(
                v["product"]["title"]), "userErrors": []}}
        if "productVariantsBulkUpdate" in q:
            var = v["variants"][0]
            return {"productVariantsBulkUpdate": {"productVariants": [
                {"id": var["id"], "sku": var["inventoryItem"].get("sku"),
                 "price": var["price"]}], "userErrors": []}}
        if "productUpdate" in q:
            if self.fail_publish:
                self.fail_publish -= 1
                return {"productUpdate": {"product": None, "userErrors": [
                    {"field": ["status"], "message": "boom"}]}}
            p = self._product(status=v["product"].get("status", "DRAFT"))
            p["onlineStoreUrl"] = "https://store.example/products/t"
            return {"productUpdate": {"product": p, "userErrors": []}}
        if "publications" in q:
            return {"publications": {"nodes": [
                {"id": "gid://shopify/Publication/7", "name": "Online Store"}]}}
        if "publishablePublish" in q:
            return {"publishablePublish": {"userErrors": []}}
        if "shop {" in q:
            return {"shop": {"name": "Demo", "myshopifyDomain": "demo.myshopify.com",
                             "currencyCode": "USD", "plan": {"displayName": "Basic"}}}
        raise AssertionError(f"unexpected query: {q[:80]}")

    def queries(self, needle):
        return [c for c in self.calls if needle in c[0]]


def _adapter(fake, **kw):
    kw.setdefault("access_token", "shpat_test")
    return ShopifyCommerceAdapter("demo.myshopify.com", transport=fake,
                                  sleep=lambda s: None, **kw)


def test_adapter_requires_credentials():
    with pytest.raises(ShopifyCredentialsError):
        ShopifyCommerceAdapter("demo.myshopify.com")


def test_create_product_is_draft_with_price_cost_seo_media():
    fake = FakeShopify()
    created = _adapter(fake).create_product({
        "title": "Posture Corrector", "description_html": "<p>x</p>",
        "price_usd": 23.99, "cost_usd": 8.3, "sku": "PC-A",
        "tags": ["posture"], "seo_title": "S", "seo_description": "D",
        "image_urls": ["https://img.example/a.jpg"],
    })
    (_, v, h), = fake.queries("productCreate")
    assert h["X-Shopify-Access-Token"] == "shpat_test"
    assert v["product"]["status"] == "DRAFT"
    assert v["product"]["seo"] == {"title": "S", "description": "D"}
    assert v["media"][0]["originalSource"] == "https://img.example/a.jpg"
    (_, v2, _), = fake.queries("productVariantsBulkUpdate")
    var = v2["variants"][0]
    assert var["price"] == "23.99"
    assert var["inventoryItem"] == {"tracked": False, "sku": "PC-A", "cost": "8.30"}
    assert created["variants"]["nodes"][0]["price"] == "23.99"
    # Nothing went live.
    assert not fake.queries("publishablePublish")


def test_publish_sets_active_and_publishes_to_online_store():
    fake = FakeShopify()
    live = _adapter(fake).publish_product("1")
    (_, v, _), = fake.queries("productUpdate")
    assert v["product"] == {"id": "gid://shopify/Product/1", "status": "ACTIVE"}
    (_, v2, _), = fake.queries("publishablePublish")
    assert v2["input"] == [{"publicationId": "gid://shopify/Publication/7"}]
    assert live["status"] == "ACTIVE"


def test_user_errors_raise():
    fake = FakeShopify()
    fake.fail_publish = 1
    with pytest.raises(ShopifyError, match="boom"):
        _adapter(fake).publish_product("1")


def test_throttling_is_retried():
    fake = FakeShopify()
    fake.throttle_once = True
    assert _adapter(fake).shop_info()["name"] == "Demo"


def test_client_credentials_token_fetched_and_refreshed_on_401():
    fake = FakeShopify()
    a = _adapter(fake, access_token=None, client_id="cid", client_secret="sec")
    a.shop_info()
    assert fake.token_requests == 1
    fake.reject_token = "tok1"
    a.shop_info()
    assert fake.token_requests == 2
    assert fake.calls[-1][2]["X-Shopify-Access-Token"] == "tok2"


# -- pipeline ------------------------------------------------------------------

@pytest.fixture
def gate(tmp_path):
    return ApprovalGate(persist_path=tmp_path / "approvals.json")


def _run_source(gate, tmp_path):
    return source(_brief(), gate, tmp_path / "reports", ListingWriter(),
                  now=datetime(2026, 10, 5, tzinfo=timezone.utc))


def test_source_opens_one_approval_per_supplier_and_publishes_nothing(gate, tmp_path):
    out = _run_source(gate, tmp_path)
    pending = gate.pending()
    assert len(pending) == 2
    assert {a.action for a in pending} == {APPROVAL_ACTION}
    top = next(a for a in pending if a.details["rank"] == 1)
    assert top.details["product"]["price_usd"] == 23.99
    assert top.details["product"]["seo_title"]
    report = Path(out["report_path"])
    assert report.name == "supplier_report_posture-corrector-2026-10_20261005.md"
    text = report.read_text(encoding="utf-8")
    assert "Listing draft" in text and "Total AI spend this mission" in text


def test_nothing_published_until_approved(gate, tmp_path):
    _run_source(gate, tmp_path)
    fake = FakeShopify()
    assert publish_approved(gate, _adapter(fake), tmp_path / "state.json") == []
    assert fake.calls == []


def test_approved_supplier_published_once_and_only_one_per_brief(gate, tmp_path):
    _run_source(gate, tmp_path)
    a, b = sorted(gate.pending(), key=lambda x: x.details["rank"])
    gate.decide(a.id, True, "owner")
    gate.decide(b.id, True, "owner")  # owner approved both by mistake
    fake = FakeShopify()
    state = tmp_path / "state.json"
    results = publish_approved(gate, _adapter(fake), state)
    by_id = {r["approval_id"]: r for r in results}
    assert by_id[a.id]["status"] == "published"
    assert by_id[a.id]["url"] == "https://store.example/products/t"
    assert by_id[b.id]["status"] == "skipped"
    assert len(fake.queries("productCreate")) == 1
    # Second run is a no-op.
    assert publish_approved(gate, _adapter(fake), state) == []
    assert len(fake.queries("productCreate")) == 1


def test_rejected_supplier_never_published(gate, tmp_path):
    _run_source(gate, tmp_path)
    for appr in gate.pending():
        gate.decide(appr.id, False, "owner")
    fake = FakeShopify()
    assert publish_approved(gate, _adapter(fake), tmp_path / "s.json") == []
    assert fake.calls == []


def test_publish_failure_resumes_without_duplicate_product(gate, tmp_path):
    _run_source(gate, tmp_path)
    top = next(a for a in gate.pending() if a.details["rank"] == 1)
    gate.decide(top.id, True, "owner")
    fake = FakeShopify()
    fake.fail_publish = 1
    state = tmp_path / "state.json"
    (r1,) = publish_approved(gate, _adapter(fake), state)
    assert r1["status"] == "draft_created" and "boom" in r1["error"]
    (r2,) = publish_approved(gate, _adapter(fake), state)
    assert r2["status"] == "published" and "error" not in r2
    assert len(fake.queries("productCreate")) == 1


def test_llm_listing_is_clipped_and_falls_back(gate):
    class Gw:
        def complete(self, **kw):
            return {"json": {"title": "T", "seo_title": "x" * 200,
                             "seo_description": "y" * 400,
                             "tags": ["A", "a", " b "]},
                    "cost_usd": 0.001, "input_tokens": 10, "output_tokens": 5}

    w = ListingWriter(Gw())
    out = w.write(_brief())
    assert len(out["seo_title"]) == 70 and len(out["seo_description"]) == 160
    assert out["tags"] == ["a", "b"]
    assert w.spend_usd == 0.001

    class Broken:
        def complete(self, **kw):
            raise RuntimeError("down")

    assert ListingWriter(Broken()).write(_brief())["title"] == "Adjustable Posture Corrector"


def test_dashboard_shows_supplier_approval(gate, tmp_path):
    from fastapi.testclient import TestClient

    from dashboard.app import create_app

    _run_source(gate, tmp_path)
    r = TestClient(create_app(approvals=gate)).get("/approvals")
    assert r.status_code == 200
    assert "Supplier #1" in r.text
    assert "$23.99" in r.text
    assert "supplier_report_posture-corrector-2026-10_20261005.md" in r.text
