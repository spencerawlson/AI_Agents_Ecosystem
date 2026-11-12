"""Agent-made videos: reviewed in Approvals, played and downloaded by their owner."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from test_saas_shell import _account, _login, client, config, store  # noqa: F401
import ventoryn.saas_routes as routes
from ventoryn import autopilot, site_jobs
from ventoryn.videos import video_path


@pytest.fixture()
def reports(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "REPORTS_DIR", tmp_path)
    return tmp_path


def _video_job(store, acct, site, reports, name="v1"):
    (reports / "videos").mkdir(exist_ok=True)
    mp4 = reports / "videos" / f"{name}.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    (reports / f"video_{name}.md").write_text(f"# Video\n\n- Video: `{mp4}`\n", encoding="utf-8")
    job = store.charge_credits(acct["id"], "video_render", site["id"], is_operator=True)
    store.set_job_result(job["id"], "done", f"Video rendered (40s, 5 scenes) · report video_{name}.md")
    return store.get_job(job["id"])


def test_video_in_approvals_plays_and_can_be_approved(client, store, reports):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://www.road2cissp.com")
    job = _video_job(store, acct, site, reports)
    html = client.get("/approvals").text
    assert "<h2>Videos to review</h2>" in html
    src = f"/websites/sites/{site['id']}/videos/{job['id']}"
    assert f"src='{src}'" in html and "Waiting for review" in html
    r = client.get(src)
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert "attachment" in client.get(src + "?download=1").headers["content-disposition"]
    assert "1 video waiting for your review." in client.get("/alerts").text
    client.post(src + "/review", data={"decision": "approve"})
    assert store.video_review_status(job["id"]) == "approved"
    assert "badge-ok'>Approved" in client.get("/approvals").text


def test_rejected_video_leaves_approvals(client, store, reports):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://www.road2cissp.com")
    job = _video_job(store, acct, site, reports)
    client.post(f"/websites/sites/{site['id']}/videos/{job['id']}/review", data={"decision": "reject"})
    assert "Videos to review" not in client.get("/approvals").text


def test_someone_elses_video_is_404(client, store, reports):
    _login(client, store)
    other = _account(store, sub="s2", email="bo@example.com")
    site = store.create_site(other["id"], "https://blog.example.org")
    job = _video_job(store, other, site, reports)
    assert client.get(f"/websites/sites/{site['id']}/videos/{job['id']}").status_code == 404
    r = client.post(f"/websites/sites/{site['id']}/videos/{job['id']}/review", data={"decision": "approve"})
    assert r.status_code == 404 and store.video_review_status(job["id"]) == "pending"


def test_report_cannot_point_outside_the_videos_folder(store, reports, tmp_path_factory):
    acct = _account(store)
    site = store.create_site(acct["id"], "https://shop.example.com")
    secret = tmp_path_factory.mktemp("elsewhere") / "secret.mp4"
    secret.write_bytes(b"x")
    (reports / "video_evil.md").write_text(f"- Video: `{secret}`\n", encoding="utf-8")
    job = store.charge_credits(acct["id"], "video_render", site["id"], is_operator=True)
    store.set_job_result(job["id"], "done", "Video rendered · report video_evil.md")
    assert video_path(store.get_job(job["id"]), reports) is None


def test_weekly_video_only_with_a_presenter_and_one_at_a_time(store, reports, monkeypatch):
    monkeypatch.setattr(site_jobs, "server_blocker", lambda jt: None)
    acct = _account(store)
    site = store.create_site(acct["id"], "https://www.road2cissp.com")
    now = datetime.now(timezone.utc)
    assert "video_render" not in autopilot.due_jobs(store, store.get_site(site["id"]), now)
    store.set_site_video(site["id"], "", "jerrod1")
    assert "video_render" in autopilot.due_jobs(store, store.get_site(site["id"]), now)
    job = _video_job(store, acct, site, reports)
    later = datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert "video_render" not in autopilot.due_jobs(store, store.get_site(site["id"]), later)
    store.set_video_review(job["id"], acct["id"], "approved")
    assert "video_render" in autopilot.due_jobs(store, store.get_site(site["id"]), later)
