"""Competitor tracking + trend analysis (Holo gap 3).

No network, no real credentials, no spend, no publishing anywhere in
this file — the DataForSEO adapter runs against a fake client, the
trends adapter against a fake trends client, and the store is
in-memory. The one real rule under test: nothing is ever invented.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ecosystem import daily_plan
from businesses.road2cissp.growth import CADENCE, RETRY_AFTER
from ventoryn.competitor_trends import (
    NO_DATA_MESSAGE, DataForSEOAdapter, LocalTrendsAdapter,
    findings_for_plan, refresh_competitors, state_for_plan,
)
from ventoryn.saas import SaasError, SaasStore

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
SITE_URL = "https://road2cissp.com"


def _store_with_site(url=SITE_URL):
    store = SaasStore(":memory:")
    account = store.create_account("sub-1", "owner@example.com")
    site = store.create_site(account["id"], url)
    return store, site


# -- fakes ---------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeDataForSEOClient:
    """httpx-like fake: records calls, answers per endpoint."""

    def __init__(self, competitors_body=None, keywords_body=None,
                 status=200):
        self.calls = []
        self.competitors_body = competitors_body or {"tasks": []}
        self.keywords_body = keywords_body or {"tasks": []}
        self.status = status

    def post(self, url, json=None, auth=None):
        self.calls.append({"url": url, "json": json, "auth": auth})
        if "competitors_domain" in url:
            return _FakeResponse(self.status, self.competitors_body)
        return _FakeResponse(self.status, self.keywords_body)


class _FakeTrendsClient:
    def __init__(self, interest):
        self._interest = interest

    def interest(self, keywords):
        return {k: v for k, v in self._interest.items() if k in keywords}


class _FakeProvider:
    """A provider that returns exactly what it is told to."""

    def __init__(self, report):
        self._report = report

    @property
    def configured(self):
        return True

    def fetch(self, competitors, keywords):
        return self._report


def _finding(**over):
    f = {"competitor": "Rival", "kind": "post",
         "title": "Rival launches a free CISSP quiz",
         "text": "New free quiz on their homepage.",
         "url": "https://rival.example/quiz",
         "observed_at": NOW.isoformat(), "source": "fake"}
    f.update(over)
    return f


# -- DataForSEO adapter ---------------------------------------------------------

def test_dataforseo_adapter_not_configured_without_env():
    adapter = DataForSEOAdapter(client=_FakeDataForSEOClient(), env={})
    assert adapter.configured is False
    report = adapter.fetch([{"name": "Rival", "url": "https://rival.example"}],
                           ["cissp course"])
    assert report["status"] == "not_configured"
    assert report["findings"] == [] and report["trends"] == []
    assert "50" in report["reason"]  # the Spencer step is named
    # And a refresh through it stores nothing.
    store, site = _store_with_site()
    store.add_competitor(site["id"], "Rival", "https://rival.example")
    assert refresh_competitors(store, site["id"], adapter,
                               ["cissp course"])["status"] == "not_configured"
    assert store.list_competitor_findings(site["id"]) == []
    assert store.competitor_summary(site["id"])["message"] == NO_DATA_MESSAGE


def test_dataforseo_adapter_configured_parses_fake_response():
    comp_body = {"tasks": [{"result": [
        {"domain": "rival2.example", "intersections": 12,
         "avg_position": 4.5},
        {"domain": "rival3.example", "intersections": 3}]}]}
    kw_body = {"tasks": [{"result": [
        {"keyword": "cissp course", "search_volume": 1300},
        {"keyword": "cissp practice test"}]}]}
    client = _FakeDataForSEOClient(comp_body, kw_body)
    adapter = DataForSEOAdapter(
        client=client,
        env={"DATAFORSEO_LOGIN": "login", "DATAFORSEO_PASSWORD": "pw"})
    assert adapter.configured is True
    report = adapter.fetch(
        [{"name": "Rival", "url": "https://rival.example"}],
        ["cissp course", "cissp practice test"])
    assert report["status"] == "ok"
    assert report["source"] == "dataforseo"
    titles = [f["title"] for f in report["findings"]]
    assert any("rival2.example" in t and "12" in t for t in titles)
    assert any("rival3.example" in t for t in titles)
    assert any("cissp course" in t and "1300" in t for t in titles)
    assert any("cissp practice test" in t for t in titles)
    # Credentials travelled only as the request auth tuple, never in a
    # finding, and the client saw both endpoints.
    assert all("login" not in f["title"] and "pw" not in f["title"]
               for f in report["findings"])
    assert {c["auth"] for c in client.calls} == {("login", "pw")}
    assert any("competitors_domain" in c["url"] for c in client.calls)
    assert any("keyword_overview" in c["url"] for c in client.calls)


def test_dataforseo_adapter_api_failure_is_unavailable_not_empty():
    client = _FakeDataForSEOClient(status=401)
    adapter = DataForSEOAdapter(
        client=client,
        env={"DATAFORSEO_LOGIN": "login", "DATAFORSEO_PASSWORD": "pw"})
    report = adapter.fetch([{"name": "Rival", "url": "https://rival.example"}],
                           ["cissp course"])
    assert report["status"] == "unavailable"
    assert report["findings"] == []


def test_dataforseo_adapter_malformed_body_yields_no_findings():
    client = _FakeDataForSEOClient(
        competitors_body={"tasks": [{"result": "not a list"}]},
        keywords_body={"no": "tasks key"})
    adapter = DataForSEOAdapter(
        client=client,
        env={"DATAFORSEO_LOGIN": "login", "DATAFORSEO_PASSWORD": "pw"})
    report = adapter.fetch([{"name": "Rival", "url": "https://rival.example"}],
                           ["cissp course"])
    assert report["status"] == "ok"
    assert report["findings"] == []  # defensive parse, never invented


# -- local trends adapter ---------------------------------------------------------

def test_local_trends_adapter_rising_and_falling_become_findings():
    trends = _FakeTrendsClient({
        "cissp course": {"trend": "rising", "avg_12mo": 40, "latest": 70},
        "free quiz": {"trend": "flat", "avg_12mo": 30, "latest": 31},
        "cissp exam": {"trend": "falling", "avg_12mo": 60, "latest": 40},
    })
    report = LocalTrendsAdapter(trends).fetch([], ["cissp course",
                                                   "free quiz",
                                                   "cissp exam"])
    assert report["status"] == "ok"
    assert len(report["trends"]) == 3
    titles = [f["title"] for f in report["findings"]]
    assert any("cissp course" in t and "rising" in t for t in titles)
    assert any("cissp exam" in t and "falling" in t for t in titles)
    assert not any("free quiz" in t for t in titles)  # flat: no move
    assert all(f["kind"] == "trend" for f in report["findings"])


def test_local_trends_adapter_without_client_is_unavailable():
    report = LocalTrendsAdapter(None).fetch([], ["cissp course"])
    assert report["status"] == "unavailable"
    assert report["findings"] == []


def test_local_trends_adapter_without_keywords_is_unavailable():
    report = LocalTrendsAdapter(_FakeTrendsClient({})).fetch([], [])
    assert report["status"] == "unavailable"


# -- provider abstraction + refresh --------------------------------------------

def test_fake_provider_refresh_stores_findings_and_dedupes():
    store, site = _store_with_site()
    comp = store.add_competitor(site["id"], "Rival", "https://rival.example")
    report = refresh_competitors(
        store, site["id"],
        _FakeProvider({"source": "fake", "status": "ok", "reason": "ok",
                       "findings": [_finding(), _finding(
                           title="Rival cuts prices",
                           url="https://rival.example/prices")],
                       "trends": []}),
        ["cissp course"])
    assert report["status"] == "ok"
    assert len(store.list_competitor_findings(site["id"])) == 2
    # A second identical refresh stores nothing new.
    refresh_competitors(
        store, site["id"],
        _FakeProvider({"source": "fake", "status": "ok", "reason": "ok",
                       "findings": [_finding()], "trends": []}), [])
    assert len(store.list_competitor_findings(site["id"])) == 2
    # Findings keep the rival they came from.
    assert store.list_competitor_findings(site["id"])[0]["competitor"] == "Rival"
    assert comp["name"] == "Rival"


def test_refresh_stores_nothing_when_provider_unavailable():
    store, site = _store_with_site()
    store.add_competitor(site["id"], "Rival", "https://rival.example")
    report = refresh_competitors(
        store, site["id"],
        _FakeProvider({"source": "fake", "status": "unavailable",
                       "reason": "blocked", "findings": [], "trends": []}),
        [])
    assert report["status"] == "unavailable"
    assert store.list_competitor_findings(site["id"]) == []
    # And a failed refresh never wipes findings already stored.
    store.record_competitor_findings(site["id"], [_finding()])
    refresh_competitors(
        store, site["id"],
        _FakeProvider({"source": "fake", "status": "unavailable",
                       "reason": "blocked", "findings": [], "trends": []}),
        [])
    assert len(store.list_competitor_findings(site["id"])) == 1


# -- competitor CRUD per site ----------------------------------------------------

def test_competitor_crud_per_site_and_isolation():
    store, site_a = _store_with_site()
    _, site_b = _store_with_site("https://other.example")
    a1 = store.add_competitor(site_a["id"], "Rival A", "https://rival-a.example",
                              notes="main rival")
    store.add_competitor(site_a["id"], "Rival B", "https://rival-b.example")
    store.add_competitor(site_b["id"], "Rival C", "https://rival-c.example")
    assert [c["name"] for c in store.list_competitors(site_a["id"])] == [
        "Rival A", "Rival B"]
    assert [c["name"] for c in store.list_competitors(site_b["id"])] == [
        "Rival C"]
    assert a1["notes"] == "main rival"
    # Findings are per site too.
    store.record_competitor_findings(
        site_a["id"], [_finding(competitor="Rival A")])
    assert len(store.list_competitor_findings(site_b["id"])) == 0
    # Removing a rival removes its findings, and only for its own site.
    assert store.remove_competitor(site_a["id"], a1["id"]) is True
    assert [c["name"] for c in store.list_competitors(site_a["id"])] == [
        "Rival B"]
    assert store.list_competitor_findings(site_a["id"]) == []
    assert store.remove_competitor(site_a["id"], a1["id"]) is False
    # A rival of site B cannot be removed through site A.
    rival_c = store.list_competitors(site_b["id"])[0]
    assert store.remove_competitor(site_a["id"], rival_c["id"]) is False
    assert len(store.list_competitors(site_b["id"])) == 1


def test_add_competitor_rejects_empty_name_and_bad_url():
    store, site = _store_with_site()
    with pytest.raises(SaasError, match="name"):
        store.add_competitor(site["id"], "   ")
    with pytest.raises(SaasError):
        store.add_competitor(site["id"], "Rival", "not a url")


def test_competitor_summary_honest_states():
    store, site = _store_with_site()
    summary = store.competitor_summary(site["id"])
    assert summary["status"] == "no_data"
    assert summary["message"] == NO_DATA_MESSAGE
    assert summary["competitors"] == [] and summary["findings"] == []
    store.add_competitor(site["id"], "Rival", "https://rival.example")
    # A tracked rival with no findings yet is still "no data".
    assert store.competitor_summary(site["id"])["status"] == "no_data"
    store.record_competitor_findings(site["id"], [_finding()])
    summary = store.competitor_summary(site["id"])
    assert summary["status"] == "has_data"
    assert "1 finding" in summary["message"]


def test_findings_for_plan_round_trip():
    store, site = _store_with_site()
    assert findings_for_plan(store, site["id"]) == []
    store.add_competitor(site["id"], "Rival", "https://rival.example")
    store.record_competitor_findings(site["id"], [_finding()])
    plan_state = state_for_plan(store, site["id"])
    assert len(plan_state["competitors"]) == 1
    assert len(plan_state["competitor_findings"]) == 1
    assert plan_state["competitor_findings"][0]["title"] == (
        "Rival launches a free CISSP quiz")


# -- weekly-plan integration ------------------------------------------------------

def _plan_state(**over):
    state = {"last_run": {}, "last_attempt": {}, "metrics": [],
             "experiments": {}, "strategy_history": [], "spend_usd": 0.0}
    state.update(over)
    return state


def _build(state, now=NOW):
    return daily_plan.build_daily_plan(
        business_name="Road to CISSP", site_url=SITE_URL, state=state,
        now=now, cadence=CADENCE, retry_after=RETRY_AFTER)


def test_plan_includes_competitor_and_trend_findings():
    state = _plan_state(
        community_threads={
            "a1": {"id": "a1", "venue": "r/cissp",
                   "title": "How do I study?",
                   "url": "https://www.reddit.com/r/cissp/comments/a1/x/",
                   "intent_score": 85, "intent": "high",
                   "reasons": ["asks a question"], "status": "drafted",
                   "draft_path": "reports/community_drafts/a1.md",
                   "created_utc": NOW.timestamp()},
        },
        competitor_findings=[
            {"id": "f1", "competitor": "Rival", "kind": "post",
             "title": "Rival launches a free CISSP quiz",
             "text": "New free quiz on their homepage.",
             "url": "https://rival.example/quiz", "source": "fake",
             "observed_at": NOW.isoformat()},
            {"id": "f2", "competitor": "", "kind": "trend",
             "title": "'cissp course' is rising (12-month avg 40, latest 70)",
             "text": "Google Trends, 12-month interest direction.",
             "url": "", "source": "local_trends",
             "observed_at": NOW.isoformat()},
        ])
    plan = _build(state)
    ids = [i["id"] for i in plan["items"]]
    assert "competitor:f1" in ids
    assert "trend:f2" in ids
    rival = next(i for i in plan["items"] if i["id"] == "competitor:f1")
    assert rival["channel"] == "competitors"
    assert "Rival" in rival["detail"]
    assert "nothing posts without your approval" in rival["action"].lower()
    assert rival["source_key"] == "competitor-finding:f1"
    trend = next(i for i in plan["items"] if i["id"] == "trend:f2")
    assert trend["channel"] == "trends"
    # Rival moves and trends outrank due jobs but not drafted replies.
    assert ids.index("reply:a1") < ids.index("competitor:f1")
    assert ids.index("competitor:f1") < ids.index("job:audit")
    assert plan["competitor_status"]["findings"] == 2
    assert plan["competitor_status"]["message"] == "2 competitor finding(s)"


def test_plan_honest_empty_state_no_competitor_items():
    plan = _build(_plan_state())
    ids = [i["id"] for i in plan["items"]]
    assert not any(i.startswith("competitor:") or i.startswith("trend:")
                   for i in ids)
    assert plan["competitor_status"] == {
        "tracked": 0, "findings": 0, "message": NO_DATA_MESSAGE}
    # The empty state survives rendering: no rival is named anywhere.
    text = daily_plan.render_text(plan)
    assert "Rival" not in text
    assert NO_DATA_MESSAGE not in text  # status is data, not a plan item


def test_plan_competitor_items_are_sticky_done():
    state = _plan_state(competitor_findings=[
        {"id": "f1", "competitor": "Rival", "kind": "post",
         "title": "Rival launches a free CISSP quiz", "text": "",
         "url": "", "source": "fake", "observed_at": NOW.isoformat()}])
    plan = _build(state)
    assert "competitor:f1" in [i["id"] for i in plan["items"]]
    daily_plan.record_plan(state, plan)
    item = next(i for i in plan["items"] if i["id"] == "competitor:f1")
    daily_plan.mark_item(state, plan["date"], "competitor:f1", "done",
                         source_key=item["source_key"])
    # Next day the done rival move stays hidden while its source is
    # unchanged — same sticky rule as drafted replies.
    from datetime import timedelta
    next_day = _build(state, now=NOW + timedelta(days=1))
    assert "competitor:f1" not in [i["id"] for i in next_day["items"]]
