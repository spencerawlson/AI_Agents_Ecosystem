"""Run agent work: every priced job is offered and runs on the site."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ventoryn.saas_routes as saas_routes
import ventoryn.site_jobs as site_jobs
from _signin import operator_store_and_config, sign_in
from ventoryn.app import create_app
from ventoryn.saas import CREDIT_COSTS, RUNNABLE_JOB_TYPES

CSV = (
    "date,time,channel,post_id,caption,post_type,likes,comments,shares,"
    "saves,reach,impressions,views,followers,row_type\n"
    "2026-11-02,09:00,instagram,ig1,Amazing new drop,photo,100,10,5,0,1000,1200,,,post\n"
    "2026-11-09,09:00,instagram,ig2,Great photos,photo,120,12,8,0,1100,1300,,,post\n"
    "2026-11-03,18:00,instagram,ig3,New stock arrived,reel,20,2,0,0,500,600,,,post\n")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    store, config = operator_store_and_config()
    app = create_app(saas_store=store, saas_config=config)
    client = TestClient(app)
    account = sign_in(client, store, email="ada@example.com", sub="sub-ada",
                      name="Ada")
    store.grant_credits(account["id"], 100, "test grant")
    site = store.create_site(account["id"], "https://example.com")
    monkeypatch.setattr(saas_routes, "REPORTS_DIR", tmp_path)
    return client, store, account, site, tmp_path


def _post(client, site, files=None, **data):
    return client.post(f"/account/sites/{site['id']}/jobs", data=data,
                       files=files, follow_redirects=False)


def test_every_priced_job_is_runnable_and_offered(env):
    client, _store, _account, site, _ = env
    assert RUNNABLE_JOB_TYPES == frozenset(CREDIT_COSTS)
    assert set(site_jobs.JOB_LABELS) == set(CREDIT_COSTS)
    html = client.get(f"/account/sites/{site['id']}").text
    for job in CREDIT_COSTS:
        assert f"value='{job}'" in html, job
    assert "coming soon" not in html.lower()
    assert " disabled>" not in html
    assert "enctype='multipart/form-data'" in html


def test_geo_audit_runs_and_charges(env, monkeypatch):
    client, store, account, site, reports = env
    from core.geo_audit import GeoAuditor
    monkeypatch.setattr(GeoAuditor, "audit", lambda self, url: {
        "score": 70, "pages_checked": 3, "has_llms_txt": False,
        "robots": {"GPTBot": "allowed"},
        "findings": [{"severity": "high", "area": "llms",
                      "issue": "no llms.txt", "fix": "add one", "pages": []}]})
    assert _post(client, site, job_type="geo_audit").status_code == 303
    assert store.credit_balance(account["id"]) == 100 - CREDIT_COSTS["geo_audit"]
    job = store.list_jobs(account["id"], site_id=site["id"])[0]
    assert job["status"] == "done" and "GEO 70/100" in job["result"]
    report = next(reports.glob("geo_example-com_*.md")).read_text(encoding="utf-8")
    assert "no llms.txt" in report and "add one" in report


def test_community_listening_needs_keywords_before_charging(env):
    client, store, account, site, _ = env
    r = _post(client, site, job_type="community_listening")
    assert r.status_code == 400 and "keyword" in r.text
    assert store.credit_balance(account["id"]) == 100
    assert store.list_jobs(account["id"]) == []


def test_community_listening_runs(env, monkeypatch):
    client, store, account, site, reports = env
    seen = {}

    def fake_listen(subs, keywords, now=None):
        seen.update(subs=subs, keywords=keywords)
        return {"threads": [{"title": "Which exam?",
                             "url": "https://reddit.com/x", "venue": "r/cissp",
                             "intent_score": 80, "intent": "high"}],
                "dropped": [],
                "sources": {"reddit": {"status": "ok", "queries": 2}}}

    monkeypatch.setattr("core.community_listening.listen_reddit", fake_listen)
    r = _post(client, site, job_type="community_listening",
              keywords="cissp exam, study plan", community="cissp")
    assert r.status_code == 303
    assert seen == {"subs": ["r/cissp"],
                    "keywords": ["cissp exam", "study plan"]}
    job = store.list_jobs(account["id"], site_id=site["id"])[0]
    assert job["status"] == "done" and "1 threads" in job["result"]
    text = next(reports.glob("listening_*.md")).read_text(encoding="utf-8")
    assert "NOTHING WAS POSTED" in text


def test_community_listening_defaults_to_all_of_reddit(env, monkeypatch):
    client, _store, _account, site, _ = env
    seen = {}
    monkeypatch.setattr(
        "core.community_listening.listen_reddit",
        lambda subs, kw, now=None: seen.update(subs=subs) or {
            "threads": [], "dropped": [],
            "sources": {"reddit": {"status": "ok", "queries": 1}}})
    assert _post(client, site, job_type="community_listening",
                 keywords="cissp").status_code == 303
    assert seen["subs"] == ["r/all"]


def test_community_listening_reddit_down_refunds(env, monkeypatch):
    client, store, account, site, _ = env
    monkeypatch.setattr(
        "core.community_listening.listen_reddit",
        lambda s, k, now=None: {"threads": [], "dropped": [],
                                "sources": {"reddit": {"status": "unavailable",
                                                       "reason": "blocked"}}})
    r = _post(client, site, job_type="community_listening", keywords="cissp")
    assert r.status_code == 500 and "refunded" in r.text
    assert store.credit_balance(account["id"]) == 100


def test_social_report_from_upload(env):
    client, store, account, site, reports = env
    r = _post(client, site, job_type="social_report",
              files={"export": ("instagram.csv", CSV.encode(), "text/csv")})
    assert r.status_code == 303, r.text
    job = store.list_jobs(account["id"], site_id=site["id"])[0]
    assert job["status"] == "done", job["result"]
    assert list(reports.glob("social_*.md"))


def test_social_report_without_file_is_not_charged(env):
    client, store, account, site, _ = env
    r = _post(client, site, job_type="social_report")
    assert r.status_code == 400 and "export file" in r.text
    assert store.credit_balance(account["id"]) == 100


@pytest.mark.parametrize("job", ["seo_article", "video_render"])
def test_drafting_jobs_run_when_the_server_can(env, monkeypatch, job):
    client, store, account, site, _ = env
    monkeypatch.setattr(site_jobs, "server_blocker", lambda jt: None)
    monkeypatch.setitem(site_jobs._RUNNERS, job,
                        lambda s, i, ctx: (f"{job} done · report x.md", "x.md"))
    assert _post(client, site, job_type=job, keywords="k").status_code == 303
    assert store.credit_balance(account["id"]) == 100 - CREDIT_COSTS[job]
    job_row = store.list_jobs(account["id"], site_id=site["id"])[0]
    assert job_row["status"] == "done"


def test_video_needs_ffmpeg(monkeypatch):
    monkeypatch.setattr(site_jobs, "_llm_enabled", lambda: True)
    monkeypatch.setattr(site_jobs.shutil, "which", lambda name: None)
    assert "ffmpeg" in site_jobs.server_blocker("video_render")


def test_another_accounts_site_cannot_run_jobs(env):
    client, store, _account, _site, _ = env
    other = store.create_account("sub-other", "bob@example.com", "Bob")
    theirs = store.create_site(other["id"], "https://bob.example.com")
    r = _post(client, theirs, job_type="geo_audit")
    assert r.status_code == 404
