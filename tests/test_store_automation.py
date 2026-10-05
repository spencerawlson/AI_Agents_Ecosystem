"""Store automation: trend discovery, CJ supplier search, ad platforms
(paused until approved), spend guard, profit, SEO, content and the full
operations cycle."""

import json
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ads import (AdCopyWriter, CampaignPlan, GoogleAdsAdapter, Insights,
                      MetaAdsAdapter, MockAdsAdapter)
from core.product_discovery import ProductIdea, SeedIdeaSource, discover
from core.sourcing import ProductBrief, Supplier
from core.supplier_search import (CJDropshippingSource, CombinedSupplierSource,
                                  SupplierSearchError)
from ecosystem.ads_pipeline import (BUDGET_ACTION, LAUNCH_ACTION, AdsPolicy,
                                    apply_approvals, campaign_key, draft_campaigns)
from ecosystem.shopify_pipeline import (APPROVAL_ACTION, discover_briefs,
                                        load_state, save_state)
from ecosystem.store_ops import (draft_articles, guard_campaigns,
                                 product_performance, run_seo, run_store_cycle,
                                 seo_audit, seo_fix)
from orchestrator.approvals import ApprovalGate

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
PID = "gid://shopify/Product/1"


# -- discovery ------------------------------------------------------------------

class FakeTrends:
    def __init__(self, data=None, fail=False):
        self.data = data or {}
        self.fail = fail
        self.calls = []

    def interest(self, kws):
        self.calls.append(list(kws))
        if self.fail:
            raise RuntimeError("429")
        return {k: self.data[k] for k in kws if k in self.data}


SEEDS = [
    {"product_name": "Desk Cable Organizer", "keywords": ["cable organizer", "desk cable management"],
     "market_price_usd": [19.99, 29.99]},
    {"product_name": "Fidget Spinner", "keywords": ["fidget spinner", "spinner toy"],
     "market_price_usd": [5, 12]},
    {"product_name": "Laptop Stand", "keywords": ["laptop stand", "adjustable laptop stand"],
     "market_price_usd": [25, 45]},
    {"product_name": "Collagen Supplement", "keywords": ["collagen supplement"],
     "market_price_usd": [20, 40]},
]


def test_discovery_drops_falling_and_restricted_ranks_rising_first():
    trends = FakeTrends({"cable organizer": {"trend": "flat", "avg_12mo": 40},
                         "fidget spinner": {"trend": "falling", "avg_12mo": 10},
                         "laptop stand": {"trend": "rising", "avg_12mo": 60}})
    briefs, ideas = discover(SeedIdeaSource(SEEDS), ["home office"], trends=trends)
    assert [b.product_name for b in briefs] == ["Laptop Stand", "Desk Cable Organizer"]
    assert briefs[0].demand_trend == "rising"
    restricted = next(i for i in ideas if i.product_name == "Collagen Supplement")
    assert any("restricted" in f for f in restricted.flags)
    # Restricted ideas never reach the Trends API.
    assert all("collagen supplement" not in c for c in trends.calls)


def test_discovery_survives_trends_outage():
    briefs, ideas = discover(SeedIdeaSource(SEEDS[:1]), [], trends=FakeTrends(fail=True))
    assert len(briefs) == 1
    assert any("not verified" in f for f in ideas[0].flags)


def test_discover_briefs_writes_files_and_keeps_owner_edits(tmp_path):
    bdir, rdir = tmp_path / "briefs", tmp_path / "reports"
    owned = ProductBrief(id="laptop-stand", niche="x", product_name="Laptop Stand",
                         suppliers=[Supplier(name="Mine", unit_cost_usd=5)])
    bdir.mkdir()
    (bdir / "laptop-stand.json").write_text(owned.model_dump_json(), encoding="utf-8")
    out = discover_briefs(SeedIdeaSource(SEEDS[:3]), ["home office"], bdir, rdir,
                          trends=None, now=NOW)
    kept = ProductBrief.model_validate_json((bdir / "laptop-stand.json").read_text(encoding="utf-8"))
    assert kept.suppliers[0].name == "Mine"
    assert (bdir / "desk-cable-organizer.json").exists()
    report = Path(out["report_path"]).read_text(encoding="utf-8")
    assert "Product Discovery" in report and "Total AI spend this mission" in report


# -- CJ supplier search ---------------------------------------------------------

class FakeCJ:
    def __init__(self):
        self.calls = []
        self.rate_limit_once = False

    def __call__(self, method, url, headers, body):
        path = urllib.parse.urlparse(url).path.split("/v1", 1)[1]
        self.calls.append((path, headers))
        if self.rate_limit_once and path != "/authentication/getAccessToken":
            self.rate_limit_once = False
            return 429, "slow down"
        ok = lambda data: (200, json.dumps({"code": 200, "result": True, "data": data}))
        if path == "/authentication/getAccessToken":
            assert json.loads(body) == {"apiKey": "k"}
            return ok({"accessToken": "cjtok"})
        if path == "/product/list":
            return ok({"list": [{"pid": "P1", "productNameEn": "Laptop Stand Alu",
                                 "sellPrice": "6.50 -- 8.00",
                                 "productImage": "https://img.cj/1.jpg"}]})
        if path == "/product/query":
            return ok({"productImage": "https://img.cj/1.jpg",
                       "productImageSet": ["https://img.cj/2.jpg"],
                       "variants": [{"vid": "V2", "variantSellPrice": "7.10"},
                                    {"vid": "V1", "variantSellPrice": "6.50"}]})
        if path == "/logistic/freightCalculate":
            assert json.loads(body)["products"] == [{"quantity": 1, "vid": "V1"}]
            return ok([{"logisticName": "CJPacket", "logisticPrice": 4.2, "logisticAging": "7-12"},
                       {"logisticName": "Slow", "logisticPrice": 9.9, "logisticAging": "20"}])
        raise AssertionError(path)


def test_cj_search_builds_priced_suppliers():
    fake = FakeCJ()
    src = CJDropshippingSource("k", transport=fake, sleep=lambda s: None)
    brief = ProductBrief(id="b", niche="n", product_name="Laptop Stand",
                         keywords=["laptop stand"])
    (sup,) = src.find(brief)
    assert sup.unit_cost_usd == 6.50 and sup.shipping_cost_usd == 4.2
    assert sup.lead_time_days == 12 and sup.platform == "cjdropshipping"
    assert sup.image_urls == ["https://img.cj/1.jpg", "https://img.cj/2.jpg"]
    assert all(h.get("CJ-Access-Token") == "cjtok" for p, h in fake.calls
               if p != "/authentication/getAccessToken")


def test_cj_rate_limit_retried():
    fake = FakeCJ()
    fake.rate_limit_once = True
    src = CJDropshippingSource("k", transport=fake, sleep=lambda s: None)
    assert len(src.find(ProductBrief(id="b", niche="n", product_name="x"))) == 1


def test_combined_source_keeps_owner_suppliers_when_search_fails():
    class Broken:
        def find(self, brief):
            raise SupplierSearchError("CJ down")

    brief = ProductBrief(id="b", niche="n", product_name="x",
                         suppliers=[Supplier(name="Alibaba quote", unit_cost_usd=3)])
    from core.sourcing import BriefSupplierSource
    combined = CombinedSupplierSource(BriefSupplierSource(), Broken())
    assert [s.name for s in combined.find(brief)] == ["Alibaba quote"]
    assert "CJ down" in combined.errors[0]


# -- ad copy + platform adapters -------------------------------------------------

def _plan(**kw):
    copy = AdCopyWriter().write("Aluminium Laptop Stand", "remote workers",
                                ["laptop stand", "laptop riser"])
    base = dict(brief_id="laptop-stand", product_title="Aluminium Laptop Stand",
                product_url="https://shop.example/products/stand",
                image_url="https://img.cj/1.jpg", daily_budget_usd=12.5,
                duration_days=7, countries=["US", "CA"], copy=copy, max_cpa_usd=9.9)
    base.update(kw)
    return CampaignPlan(**base)


def test_ad_copy_respects_platform_limits():
    class Gw:
        def complete(self, **kw):
            return {"json": {"headlines": ["x" * 50, "Dup", "Dup"],
                             "descriptions": ["d" * 200],
                             "primary_texts": [], "keywords": ["A", "a"]},
                    "cost_usd": 0.002}

    c = AdCopyWriter(Gw()).write("T", "n", ["k"])
    assert all(len(h) <= 30 for h in c.headlines) and len(c.headlines) >= 3
    assert len(set(c.headlines)) == len(c.headlines)
    assert all(len(d) <= 90 for d in c.descriptions) and len(c.descriptions) >= 2
    assert c.primary_texts and c.keywords == ["a"]


class RecordingTransport:
    def __init__(self, responder):
        self.calls = []
        self.responder = responder

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.responder(method, url, body)


def _form(body):
    return {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}


def test_meta_creates_everything_paused_and_activates_children_first():
    counter = iter(range(100, 200))

    def respond(method, url, body):
        if "/insights" in url:
            return 200, json.dumps({"data": [{
                "spend": "40.5", "impressions": "1000", "clicks": "30",
                "actions": [{"action_type": "purchase", "value": "3"},
                            {"action_type": "omni_purchase", "value": "3"}],
                "action_values": [{"action_type": "purchase", "value": "71.97"}]}]})
        return 200, json.dumps({"id": str(next(counter)), "success": True})

    t = RecordingTransport(respond)
    meta = MetaAdsAdapter("tok", "123", "page1", "pix1", transport=t)
    refs = meta.create_paused(_plan())
    forms = [_form(c[3]) for c in t.calls]
    assert [f["status"] for f in forms if "status" in f] == ["PAUSED"] * 3
    assert forms[0]["objective"] == "OUTCOME_SALES"
    assert forms[1]["daily_budget"] == "1250"
    assert json.loads(forms[1]["targeting"])["geo_locations"]["countries"] == ["US", "CA"]
    story = json.loads(forms[2]["object_story_spec"])
    assert story["page_id"] == "page1" and story["link_data"]["link"].endswith("/stand")
    assert "act_123/campaigns" in t.calls[0][1]

    t.calls.clear()
    meta.activate(refs)
    targets = [c[1].rsplit("/", 1)[-1] for c in t.calls]
    assert targets == [refs["ad_id"], refs["adset_id"], refs["campaign_id"]]
    ins = meta.insights(refs)
    assert ins.purchases == 3 and ins.revenue_usd == 71.97 and ins.spend_usd == 40.5


def test_google_builds_paused_search_campaign():
    def respond(method, url, body):
        if "oauth2" in url:
            return 200, json.dumps({"access_token": "gtok"})
        if url.endswith("googleAds:search"):
            return 200, json.dumps({"results": [{"metrics": {
                "costMicros": "25000000", "impressions": "900", "clicks": "40",
                "conversions": 2.0, "conversionsValue": 47.98}}]})
        return 200, json.dumps({"mutateOperationResponses": [
            {"campaignBudgetResult": {"resourceName": "customers/9/campaignBudgets/5"}},
            {"campaignResult": {"resourceName": "customers/9/campaigns/77"}}]})

    t = RecordingTransport(respond)
    g = GoogleAdsAdapter("dev", "cid", "sec", "ref", "9", transport=t)
    refs = g.create_paused(_plan())
    assert refs == {"budget": "customers/9/campaignBudgets/5",
                    "campaign": "customers/9/campaigns/77", "campaign_id": "77"}
    _, url, headers, body = t.calls[1]
    assert headers["developer-token"] == "dev" and headers["Authorization"] == "Bearer gtok"
    ops = json.loads(body)["mutateOperations"]
    camp = ops[1]["campaignOperation"]["create"]
    assert camp["status"] == "PAUSED" and camp["advertisingChannelType"] == "SEARCH"
    assert ops[0]["campaignBudgetOperation"]["create"]["amountMicros"] == "12500000"
    geos = [o["campaignCriterionOperation"]["create"]["location"]["geoTargetConstant"]
            for o in ops if "campaignCriterionOperation" in o]
    assert geos == ["geoTargetConstants/2840", "geoTargetConstants/2124"]
    rsa = next(o for o in ops if "adGroupAdOperation" in o)["adGroupAdOperation"]["create"]["ad"]
    assert all(len(h["text"]) <= 30 for h in rsa["responsiveSearchAd"]["headlines"])
    assert any("adGroupCriterionOperation" in o for o in ops)
    ins = g.insights(refs)
    assert ins.spend_usd == 25.0 and ins.purchases == 2.0


# -- ads pipeline + spend guard -------------------------------------------------

def _state_with_product(tmp_path):
    path = tmp_path / "state.json"
    state = load_state(path)
    state["products"][PID] = {
        "brief_id": "laptop-stand", "approval_id": "appr_x",
        "title": "Aluminium Laptop Stand", "url": "https://shop.example/products/stand",
        "image_url": "https://img.cj/1.jpg", "niche": "remote workers",
        "keywords": ["laptop stand"], "facts": "", "price_usd": 29.99,
        "landed_cost_usd": 10.7, "fees_usd": 1.17, "max_ad_cost_per_order_usd": 12.12,
        "published_at": "2026-10-01T00:00:00+00:00",
    }
    return path, state


@pytest.fixture
def gate(tmp_path):
    return ApprovalGate(persist_path=tmp_path / "approvals.json")


def test_ads_drafted_paused_and_only_go_live_on_approval(tmp_path, gate):
    path, state = _state_with_product(tmp_path)
    mock = MockAdsAdapter()
    res = draft_campaigns(state, PID, {"mock": mock}, gate, reports_dir=tmp_path / "r",
                          policy=AdsPolicy(default_daily_usd=10, default_days=7), now=NOW)
    assert res[0]["status"] == "drafted"
    (appr,) = gate.pending()
    assert appr.action == LAUNCH_ACTION and appr.amount_usd == 70.0
    assert mock.campaigns["mock_1"]["status"] == "PAUSED"
    assert (tmp_path / "r" / "ads_report_laptop-stand_20261005.md").exists()
    # Idempotent: no second campaign for the same product/platform.
    assert draft_campaigns(state, PID, {"mock": mock}, gate) == []
    # Pending approval -> still paused.
    apply_approvals(state, {"mock": mock}, gate)
    assert mock.campaigns["mock_1"]["status"] == "PAUSED"
    gate.decide(appr.id, True, "owner")
    (r,) = apply_approvals(state, {"mock": mock}, gate)
    assert r["status"] == "active" and mock.campaigns["mock_1"]["status"] == "ACTIVE"
    assert state["campaigns"][campaign_key(PID, "mock")]["approved_total_usd"] == 70.0
    assert apply_approvals(state, {"mock": mock}, gate) == []


def test_rejected_launch_stays_paused(tmp_path, gate):
    _, state = _state_with_product(tmp_path)
    mock = MockAdsAdapter()
    draft_campaigns(state, PID, {"mock": mock}, gate)
    gate.decide(gate.pending()[0].id, False, "owner")
    apply_approvals(state, {"mock": mock}, gate)
    assert mock.campaigns["mock_1"]["status"] == "PAUSED"
    assert state["campaigns"][campaign_key(PID, "mock")]["status"] == "rejected"


def _live_campaign(tmp_path, gate):
    _, state = _state_with_product(tmp_path)
    mock = MockAdsAdapter()
    draft_campaigns(state, PID, {"mock": mock}, gate,
                    policy=AdsPolicy(default_daily_usd=10, default_days=7))
    gate.decide(gate.pending()[0].id, True, "owner")
    apply_approvals(state, {"mock": mock}, gate)
    return state, mock


@pytest.mark.parametrize("ins,reason", [
    (Insights(spend_usd=25.0, purchases=0), "no sales"),
    (Insights(spend_usd=70.0, purchases=8, revenue_usd=240), "approved budget"),
    (Insights(spend_usd=60.0, purchases=3, revenue_usd=90), "above ceiling"),
])
def test_guard_pauses_losing_or_finished_campaigns(tmp_path, gate, ins, reason):
    state, mock = _live_campaign(tmp_path, gate)
    mock.stats["mock_1"] = ins
    (action,) = guard_campaigns(state, {"mock": mock}, gate, AdsPolicy())
    assert action["action"] == "paused" and reason in action["reason"]
    assert mock.campaigns["mock_1"]["status"] == "PAUSED"


def test_guard_proposes_scaling_winners_and_applies_on_approval(tmp_path, gate):
    state, mock = _live_campaign(tmp_path, gate)
    mock.stats["mock_1"] = Insights(spend_usd=20.0, purchases=4, revenue_usd=120)  # CPA $5
    (action,) = guard_campaigns(state, {"mock": mock}, gate, AdsPolicy())
    assert action["action"] == "scale_proposed"
    # Only one open proposal at a time.
    assert guard_campaigns(state, {"mock": mock}, gate, AdsPolicy()) == []
    (prop,) = gate.pending()
    assert prop.action == BUDGET_ACTION and prop.amount_usd == 35.0
    assert mock.campaigns["mock_1"]["daily_budget_usd"] == 10  # unchanged until approved
    gate.decide(prop.id, True, "owner")
    (r,) = apply_approvals(state, {"mock": mock}, gate)
    assert r["status"] == "budget_increased"
    assert mock.campaigns["mock_1"]["daily_budget_usd"] == 15.0
    assert state["campaigns"][campaign_key(PID, "mock")]["approved_total_usd"] == 105.0


# -- profit, SEO, content --------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.products = {}
        self.orders = []
        self.seo_updates = []
        self.articles = []
        self.counter = 0

    def create_product(self, product):
        self.counter += 1
        pid = f"gid://shopify/Product/{self.counter}"
        self.products[pid] = {
            "id": pid, "title": product["title"], "status": "DRAFT", "tags": product["tags"],
            "seo": {"title": product["seo_title"], "description": product["seo_description"]},
            "descriptionHtml": product["description_html"],
            "media": {"nodes": [{"alt": product["image_alt"]} for _ in product["image_urls"]]},
            "handle": "stand", "onlineStoreUrl": None}
        return self.products[pid]

    def publish_product(self, pid):
        p = self.products[pid]
        p.update(status="ACTIVE", onlineStoreUrl=f"https://shop.example/products/{p['handle']}")
        return p

    def get_product(self, pid):
        return self.products.get(pid)

    def list_orders_since(self, since):
        return self.orders

    def update_seo(self, pid, title, desc):
        self.seo_updates.append((pid, title, desc))
        self.products[pid]["seo"] = {"title": title, "description": desc}

    def create_article(self, title, body, tags, summary="", published=True):
        self.articles.append({"title": title, "published": published})
        return {"id": f"gid://shopify/Article/{len(self.articles)}", "title": title}


def _order(pid, qty, status="PAID"):
    return {"displayFinancialStatus": status,
            "lineItems": {"nodes": [{"quantity": qty, "product": {"id": pid}}]}}


def test_product_performance_net_of_ads_and_refunds(tmp_path, gate):
    state, mock = _live_campaign(tmp_path, gate)
    state["campaigns"][campaign_key(PID, "mock")]["insights"] = {"spend_usd": 30.0}
    store = FakeStore()
    store.orders = [_order(PID, 2), _order(PID, 1), _order(PID, 5, "REFUNDED"),
                    _order("gid://shopify/Product/999", 4)]
    perf = product_performance(state, store)[PID]
    unit = 29.99 - 10.7 - 1.17
    assert perf["units"] == 3
    assert perf["revenue_usd"] == round(3 * 29.99, 2)
    assert perf["net_profit_usd"] == round(3 * unit - 30.0, 2)


def test_seo_audit_and_safe_autofix():
    detail = {"title": "Aluminium Laptop Stand", "tags": ["a"],
              "seo": {"title": "", "description": "short"},
              "descriptionHtml": "<p>Holds laptops 10-17 in. Folds flat.</p>",
              "media": {"nodes": [{"alt": ""}]}}
    issues = seo_audit(detail)
    assert {"seo_title_missing", "seo_description_length", "thin_description",
            "image_alt_missing", "few_tags"} <= set(issues)
    fix = seo_fix(detail, {"keywords": ["laptop stand"], "niche": "remote workers"}, issues)
    assert fix["title"] == "Aluminium Laptop Stand"  # keyword already in title
    assert 50 <= len(fix["description"]) <= 160
    assert seo_fix(detail, {}, ["thin_description"]) is None  # copy never auto-changed


def test_run_seo_only_touches_seo_fields(tmp_path):
    _, state = _state_with_product(tmp_path)
    store = FakeStore()
    store.products[PID] = {"title": "Stand", "tags": [], "descriptionHtml": "",
                           "seo": {"title": "x" * 90, "description": "y" * 100},
                           "media": {"nodes": []}}
    (entry,) = run_seo(state, store, NOW)
    assert len(store.seo_updates) == 1
    assert len(store.seo_updates[0][1]) <= 70
    assert store.products[PID]["title"] == "Stand"
    assert state["seo_fixes"][PID]


def test_articles_hidden_once_per_product_and_need_llm(tmp_path):
    _, state = _state_with_product(tmp_path)
    store = FakeStore()
    assert draft_articles(state, store, None, NOW) == ([], 0.0)

    class Gw:
        def complete(self, **kw):
            return {"json": {"title": "How to Choose a Laptop Stand", "body_html": "<p>x</p>",
                             "summary": "s", "tags": ["guide"]}, "cost_usd": 0.01}

    res, spend = draft_articles(state, store, Gw(), NOW)
    assert len(res) == 1 and spend == 0.01
    assert store.articles == [{"title": "How to Choose a Laptop Stand", "published": False}]
    assert draft_articles(state, store, Gw(), NOW)[0] == []


# -- full cycle -----------------------------------------------------------------

def test_full_cycle_from_supplier_approval_to_guarded_ads(tmp_path, gate):
    from ecosystem.shopify_pipeline import ListingWriter, source

    brief = ProductBrief.model_validate_json(
        (Path(__file__).resolve().parent.parent / "examples" / "shopify_brief.example.json").read_text())
    brief.suppliers[0].image_urls = ["https://img.example/a.jpg"]
    source(brief, gate, tmp_path / "reports", ListingWriter(), now=NOW)
    top = next(a for a in gate.pending() if a.details["rank"] == 1)
    gate.decide(top.id, True, "owner")
    for a in gate.pending():
        gate.decide(a.id, False, "owner")

    store, mock = FakeStore(), MockAdsAdapter()
    state_path = tmp_path / "state.json"
    s1 = run_store_cycle(gate, store, {"mock": mock}, state_path, tmp_path / "reports",
                         policy=AdsPolicy(), now=NOW)
    assert s1["published"][0]["status"] == "published"
    assert s1["drafted"][0]["status"] == "drafted"
    assert mock.campaigns["mock_1"]["status"] == "PAUSED"
    assert not s1["errors"]
    report = Path(s1["report_path"]).read_text(encoding="utf-8")
    assert "Store Report" in report and "Adjustable Posture Corrector" in report

    (launch,) = [a for a in gate.pending() if a.action == LAUNCH_ACTION]
    gate.decide(launch.id, True, "owner")
    s2 = run_store_cycle(gate, store, {"mock": mock}, state_path, tmp_path / "reports",
                         policy=AdsPolicy(), optimize=False, now=NOW)
    assert s2["ads"][0]["status"] == "active" and s2["drafted"] == []
    assert mock.campaigns["mock_1"]["status"] == "ACTIVE"

    mock.stats["mock_1"] = Insights(spend_usd=30.0, purchases=0)
    s3 = run_store_cycle(gate, store, {"mock": mock}, state_path, tmp_path / "reports",
                         policy=AdsPolicy(), now=NOW)
    assert s3["guard"][0]["action"] == "paused"
    assert mock.campaigns["mock_1"]["status"] == "PAUSED"
    assert load_state(state_path)["campaigns"]["gid://shopify/Product/1::mock"]["status"] == "paused_by_guard"


def test_dashboard_renders_ad_approvals(gate):
    from fastapi.testclient import TestClient

    from dashboard.app import create_app

    gate.request(LAUNCH_ACTION, amount_usd=70.0, details={
        "platform": "meta", "product_title": "Laptop Stand", "daily_budget_usd": 10,
        "duration_days": 7, "total_budget_usd": 70, "max_cpa_usd": 12.12,
        "countries": ["US"], "headlines": ["Shop Now"], "primary_text": "<b>Hi</b>"})
    gate.request(BUDGET_ACTION, amount_usd=35.0, details={
        "platform": "google", "product_title": "Laptop Stand",
        "current_daily_budget_usd": 10, "new_daily_budget_usd": 15, "cpa_usd": 5,
        "max_cpa_usd": 12.12, "roas": 6.0, "purchases": 4})
    r = TestClient(create_app(approvals=gate)).get("/approvals")
    assert "Launch Meta ads" in r.text and "$70.00 max" in r.text
    assert "&lt;b&gt;Hi&lt;/b&gt;" in r.text  # escaped
    assert "Scale Google ads" in r.text and "$15.00/day" in r.text


def test_worker_store_tick_is_noop_without_shopify(monkeypatch):
    from ecosystem.worker import run_store_tick

    monkeypatch.delenv("SHOPIFY_SHOP", raising=False)
    assert run_store_tick(object(), 1) is None
