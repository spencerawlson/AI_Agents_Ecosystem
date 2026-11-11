"""X campaigns: agent drafts -> owner approves (2FA) -> posted on schedule."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_saas_shell import _account, _login, client, config, store  # noqa: F401
from ventoryn import autopilot, campaigns, site_jobs


class FakeLLM:
    def __init__(self, posts):
        self.posts = posts

    def complete(self, prompt, **kw):
        return {"json": {"posts": self.posts}}


def _site(store, acct):
    return store.create_site(acct["id"], "https://shop.example.com")


def test_validate_keeps_only_usable_posts():
    allowed = {"https://shop.example.com/a"}
    posts = campaigns.validate_posts(
        [{"text": "Good one", "link": "https://shop.example.com/a"},
         {"text": "good ONE", "link": "x"},                       # duplicate
         {"text": "x" * 300},                                      # too long
         {"text": "Off-site link https://evil.example/x", "link": "https://evil.example"},
         "junk"],
        allowed_urls=allowed, fallback_url="https://shop.example.com", with_link=True, want=10)
    assert [p["text"] for p in posts] == ["Good one", "Off-site link"]
    assert posts[1]["link"] == "https://shop.example.com"  # never an off-site link


def test_draft_and_propose_saves_pending_posts(store):
    acct = _account(store)
    site = _site(store, acct)
    llm = FakeLLM([{"text": f"Post {i}", "link": site["url"]} for i in range(40)])
    posts = campaigns.draft_posts(site, days=30, posts_per_day=1, with_link=True,
                                  llm=llm, inventory=None)
    assert len(posts) == 30
    c = campaigns.propose(store, site, posts, days=30, posts_per_day=1,
                          with_link=True, source="manual")
    assert c["status"] == "proposed"
    assert all(d["status"] == "pending" for d in store.campaign_drafts(c["id"]))


def _proposal(store, acct, n=3):
    site = _site(store, acct)
    posts = [{"text": f"Post {i}", "link": site["url"]} for i in range(n)]
    return site, campaigns.propose(store, site, posts, days=3, posts_per_day=1,
                                   with_link=True, source="autopilot")


def test_approvals_shows_proposal_with_cost(client, store):
    acct = _login(client, store)
    site, c = _proposal(store, acct)
    html = client.get("/approvals").text
    assert "Campaign proposals (1)" in html and f"id='campaign-{c['id']}'" in html
    assert "3 X posts with a link over 3 days at $0.200 each: about $0.60." in html
    assert "X is not connected for this website" in html
    assert f"action='/campaigns/{c['id']}/approve'" in html


def test_approve_needs_fresh_2fa_then_schedules(client, store):
    from _signin import fresh_totp_code

    acct = _login(client, store)
    site, c = _proposal(store, acct)
    r = client.post(f"/campaigns/{c['id']}/approve", data={"totp_code": "000000"})
    assert r.status_code == 400 and store.get_campaign(c["id"])["status"] == "proposed"
    r = client.post(f"/campaigns/{c['id']}/approve",
                    data={"totp_code": fresh_totp_code(store, acct["id"])},
                    follow_redirects=False)
    assert r.status_code == 303
    assert store.get_campaign(c["id"])["status"] == "scheduled"
    drafts = store.campaign_drafts(c["id"])
    assert [d["status"] for d in drafts] == ["scheduled"] * 3
    assert drafts[0]["scheduled_for"] < drafts[1]["scheduled_for"] < drafts[2]["scheduled_for"]


def test_remove_one_post_and_reject(client, store):
    acct = _login(client, store)
    site, c = _proposal(store, acct)
    first = store.campaign_drafts(c["id"])[0]
    client.post(f"/campaigns/{c['id']}/posts/{first['id']}/remove")
    assert store.get_social_draft(first["id"])["status"] == "rejected"
    client.post(f"/campaigns/{c['id']}/reject")
    assert store.get_campaign(c["id"])["status"] == "rejected"
    assert all(d["status"] == "rejected" for d in store.campaign_drafts(c["id"]))


def test_someone_elses_campaign_is_404(client, store):
    _login(client, store)
    other = _account(store, sub="s2", email="bo@example.com")
    _, c = _proposal(store, other)
    assert client.post(f"/campaigns/{c['id']}/reject").status_code == 404
    assert store.get_campaign(c["id"])["status"] == "proposed"


def test_due_posts_go_out_or_are_held_with_the_reason(store, config):
    acct = _account(store)
    site, c = _proposal(store, acct, n=2)
    campaigns.schedule(store, c, now=datetime.now(timezone.utc) - timedelta(days=5))
    calls = []

    def poster(store_, config_, account, site_id, draft_id):
        calls.append(draft_id)
        if len(calls) == 2:
            return False, "X is not connected for this site."
        store_.decide_social_draft(draft_id, "posted")
        return True, "ok"

    campaigns.post_due(store, config, poster=poster)
    statuses = sorted(d["status"] for d in store.campaign_drafts(c["id"]))
    assert len(calls) == 2 and statuses == ["held", "posted"]
    held = [d for d in store.campaign_drafts(c["id"]) if d["status"] == "held"][0]
    assert "not connected" in held["note"]
    campaigns.post_due(store, config, poster=poster)
    assert len(calls) == 2  # a held post is not retried in a loop


def test_autopilot_proposes_weekly_but_one_open_proposal_at_a_time(store, monkeypatch):
    monkeypatch.setattr(site_jobs, "server_blocker", lambda jt: None)
    acct = _account(store)
    site, c = _proposal(store, acct)
    now = datetime.now(timezone.utc)
    assert "x_campaign" not in autopilot.due_jobs(store, store.get_site(site["id"]), now)
    store.set_campaign_status(c["id"], "rejected")
    assert "x_campaign" in autopilot.due_jobs(store, store.get_site(site["id"]), now)


def test_agents_page_has_start_campaign_with_site_dropdown(client, store, monkeypatch):
    monkeypatch.setattr(site_jobs, "server_blocker", lambda jt: None)
    acct = _login(client, store)
    a = _site(store, acct)
    b = store.create_site(acct["id"], "https://blog.example.org")
    html = client.get("/agents").text
    assert "<h2>Start a campaign</h2>" in html
    assert f"<option value='{a['id']}'" in html and f"<option value='{b['id']}'" in html
    assert "action='/agents/campaigns'" in html


def test_start_campaign_charges_and_drafts_in_background(client, store, monkeypatch):
    monkeypatch.setattr(site_jobs, "server_blocker", lambda jt: None)
    started = {}

    class FakeThread:
        def __init__(self, target, name, daemon, args, kwargs):
            started.update(kwargs=kwargs, args=args)

        def start(self):
            pass

    import threading
    monkeypatch.setattr(threading, "Thread", FakeThread)
    acct = _login(client, store)
    store.grant_credits(acct["id"], 100, "test")
    site = _site(store, acct)
    r = client.post("/agents/campaigns", data={"site_id": site["id"], "days": "30",
                                                "posts_per_day": "1", "with_link": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and "campaign=" in r.headers["location"]
    assert started["kwargs"]["inputs"]["source"] == "manual"
    assert started["kwargs"]["job"]["job_type"] == "x_campaign"
    assert store.credit_balance(acct["id"]) == 90


def test_cannot_start_a_campaign_for_someone_elses_site(client, store, monkeypatch):
    monkeypatch.setattr(site_jobs, "server_blocker", lambda jt: None)
    _login(client, store)
    other = _account(store, sub="s2", email="bo@example.com")
    site = _site(store, other)
    r = client.post("/agents/campaigns", data={"site_id": site["id"]})
    assert r.status_code == 404


def test_campaign_posts_are_not_listed_as_single_drafts(client, store):
    acct = _login(client, store)
    site, c = _proposal(store, acct, n=3)
    assert store.list_social_drafts(site["id"]) == []
    html = client.get("/approvals").text
    for d in store.campaign_drafts(c["id"]):
        assert f"/social/x/drafts/{d['id']}/approve" not in html
