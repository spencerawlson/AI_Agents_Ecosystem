"""TikTok: connect a website, post an approved video within TikTok's UX
rules, follow its status, serve the signed media URL, revoke on disconnect."""

import io
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import ventoryn.saas_routes as routes
from test_saas_shell import _account, _login, client, config, store  # noqa: F401
from test_video_review import _video_job
from ventoryn import tiktok as tt

CREATOR = {"creator_nickname": "Road 2 CISSP", "creator_username": "road2cissp",
           "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
           "comment_disabled": False, "duet_disabled": True, "stitch_disabled": False,
           "max_video_post_duration_sec": 600}


@pytest.fixture()
def reports(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "REPORTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def fake(monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck-test")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs-test-secret")
    monkeypatch.delenv("TIKTOK_AUDITED", raising=False)
    calls = {"publish": [], "revoke": []}
    monkeypatch.setattr(tt, "exchange_code", lambda base, code, **k: {
        "access_token": "at-1", "refresh_token": "rt-1", "open_id": "oid",
        "scope": tt.SCOPES, "expires_at": int(time.time()) + 7200,
        "refresh_expires_at": int(time.time()) + 86400})
    monkeypatch.setattr(tt, "user_info", lambda tok, **k: {
        "open_id": "oid", "display_name": "Road 2 CISSP", "avatar_url": ""})
    monkeypatch.setattr(tt, "creator_info", lambda tok, **k: dict(CREATOR))

    def publish(tok, url, *, mode, post_info=None, **k):
        calls["publish"].append({"token": tok, "url": url, "mode": mode, "post_info": post_info})
        return "pub-1"
    monkeypatch.setattr(tt, "publish", publish)
    monkeypatch.setattr(tt, "status", lambda tok, pid, **k: {"status": "PUBLISH_COMPLETE"})
    monkeypatch.setattr(tt, "revoke", lambda tok, **k: calls["revoke"].append(tok))
    return calls


def _connect(client, store, site):
    r = client.get(f"/websites/sites/{site['id']}/connections/tiktok/start",
                   follow_redirects=False)
    assert r.status_code == 303
    loc = urllib.parse.urlsplit(r.headers["location"])
    q = urllib.parse.parse_qs(loc.query)
    assert loc.netloc == "www.tiktok.com" and q["client_key"] == ["ck-test"]
    assert q["scope"] == ["user.info.basic,video.publish,video.upload"]
    assert q["redirect_uri"] == ["http://testserver/account/auth/tiktok/callback"]
    r = client.get("/account/auth/tiktok/callback",
                   params={"code": "c1", "state": q["state"][0]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/integrations/tiktok?site={site['id']}"


def _approved(store, acct, site, reports):
    job = _video_job(store, acct, site, reports)
    store.set_video_review(job["id"], acct["id"], "approved")
    return job


def test_connect_stores_encrypted_tokens_and_shows_in_integrations(client, store, reports, fake):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://www.road2cissp.com")
    assert "Connect with TikTok" in client.get(f"/integrations/tiktok?site={site['id']}").text
    _connect(client, store, site)
    conn = store.get_connection(site["id"], "tiktok")
    assert conn["status"] == "connected" and conn["note"] == "Connected as Road 2 CISSP"
    assert conn["_detail_raw"]["refresh_token"] == "rt-1"
    assert "at-1" not in (conn.get("detail") or "")             # encrypted at rest
    page = client.get("/integrations").text
    assert "TikTok" in page and "Connected on 1 of 1 website" in page


def test_callback_rejects_a_bad_or_reused_state(client, store, fake):
    acct = _login(client, store)
    store.create_site(acct["id"], "https://www.road2cissp.com")
    r = client.get("/account/auth/tiktok/callback", params={"code": "c", "state": "nope"})
    assert r.status_code == 400
    r = client.get("/account/auth/tiktok/callback", params={"error": "access_denied",
                                                            "state": "x"})
    assert r.status_code == 400 and "access_denied" in r.text


def test_posting_screen_follows_tiktok_rules(client, store, reports, fake):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://www.road2cissp.com")
    job = _approved(store, acct, site, reports)
    assert f"{job['id']}/tiktok'>Post to TikTok" in client.get("/approvals").text
    _connect(client, store, site)
    html = client.get(f"/websites/sites/{site['id']}/videos/{job['id']}/tiktok").text
    assert "Posting to TikTok as <strong>Road 2 CISSP</strong> (@road2cissp)" in html
    assert "<option value='' selected disabled>Choose who can see it</option>" in html
    # not audited yet: private only
    assert "SELF_ONLY" in html and "PUBLIC_TO_EVERYONE" not in html
    assert "name='allow_comment' value='1'>" in html            # unchecked
    assert "name='allow_duet' value='1' disabled>" in html       # creator turned duet off
    assert "checked" not in html.split("id='tt-commercial'")[1].split(">")[0]
    assert "Music Usage Confirmation" in html and "few minutes" in html
    assert "<video" in html and "maxlength='2200'" in html


def test_post_direct_then_status(client, store, reports, fake):
    from _signin import fresh_totp_code

    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://www.road2cissp.com")
    job = _approved(store, acct, site, reports)
    _connect(client, store, site)
    url = f"/websites/sites/{site['id']}/videos/{job['id']}/tiktok"
    r = client.post(url, data={"title": "CISSP in 60s", "privacy": "",
                               "totp_code": fresh_totp_code(store, acct["id"])})
    assert r.status_code == 400 and "Choose who can view" in r.text and not fake["publish"]
    r = client.post(url, data={"title": "CISSP in 60s", "privacy": "SELF_ONLY",
                               "allow_comment": "1", "allow_duet": "1", "is_aigc": "1",
                               "mode": "direct",
                               "totp_code": fresh_totp_code(store, acct["id"])},
                    follow_redirects=False)
    assert r.status_code == 303
    call = fake["publish"][0]
    assert call["mode"] == "direct" and call["token"] == "at-1"
    assert call["post_info"] == {"title": "CISSP in 60s", "privacy_level": "SELF_ONLY",
                                 "disable_comment": False, "disable_duet": True,
                                 "disable_stitch": True, "brand_organic_toggle": False,
                                 "brand_content_toggle": False, "is_aigc": True}
    assert call["url"].startswith("http://testserver/media/tiktok/")
    page = client.get(r.headers["location"]).text
    assert "Posted on TikTok" in page
    # TikTok pulls the file without a session
    client.cookies.clear()
    media = client.get(urllib.parse.urlsplit(call["url"]).path)
    assert media.status_code == 200 and media.headers["content-type"] == "video/mp4"


def test_commercial_content_rules(client, store, reports, fake):
    form = {"privacy": "SELF_ONLY", "commercial": "1"}
    assert "promotes yourself, a third party" in tt.post_info_from_form(form, CREATOR)[1]
    form["brand_content"] = "1"
    assert "cannot be set to private" in tt.post_info_from_form(form, CREATOR)[1]
    form = {"privacy": "PUBLIC_TO_EVERYONE", "commercial": "1", "brand_organic": "1"}
    info, err = tt.post_info_from_form(form, CREATOR, env={"TIKTOK_AUDITED": "1"})
    assert not err and info["brand_organic_toggle"] and not info["brand_content_toggle"]
    # before the audit, public is refused
    assert tt.post_info_from_form(form, CREATOR, env={})[1]


def test_unapproved_video_or_no_connection_cannot_post(client, store, reports, fake):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://www.road2cissp.com")
    job = _video_job(store, acct, site, reports)
    url = f"/websites/sites/{site['id']}/videos/{job['id']}/tiktok"
    assert "Connect TikTok first" in client.get(url).text
    _connect(client, store, site)
    r = client.post(url, data={"privacy": "SELF_ONLY"})
    assert r.status_code == 400 and not fake["publish"]


def test_media_token_is_signed_and_expires(client, store, reports, fake):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://www.road2cissp.com")
    job = _approved(store, acct, site, reports)
    good = tt.media_token(job["id"])
    assert tt.media_job(good) == job["id"]
    assert tt.media_job(good, now=time.time() + tt.MEDIA_TTL_S + 5) is None
    assert tt.media_job(good[:-2] + "AA") is None
    assert client.get(f"/media/tiktok/{good}.mp4").status_code == 200
    assert client.head(f"/media/tiktok/{good}.mp4").status_code == 200
    assert client.get("/media/tiktok/forged.mp4").status_code == 404
    store.set_video_review(job["id"], acct["id"], "rejected")
    assert client.get(f"/media/tiktok/{good}.mp4").status_code == 404


def test_disconnect_revokes_at_tiktok(client, store, reports, fake):
    from _signin import fresh_totp_code

    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://www.road2cissp.com")
    _connect(client, store, site)
    client.post(f"/websites/sites/{site['id']}/connections/tiktok/disconnect",
                data={"totp_code": fresh_totp_code(store, acct["id"])})
    assert fake["revoke"] == ["at-1"]
    assert store.get_connection(site["id"], "tiktok")["status"] == "not_connected"


def test_expired_access_token_is_refreshed_and_rotated(store, monkeypatch):
    acct = _account(store)
    site = store.create_site(acct["id"], "https://www.road2cissp.com")
    store.update_connection(site["id"], "tiktok", "connected", "Connected as R",
                            {"access_token": "old", "refresh_token": "rt-old",
                             "expires_at": int(time.time()) - 10})
    seen = []
    monkeypatch.setattr(tt, "refresh", lambda rt, **k: seen.append(rt) or {
        "access_token": "new", "refresh_token": "rt-new", "expires_at": int(time.time()) + 7200})
    assert tt.fresh_access_token(store, site["id"]) == "new" and seen == ["rt-old"]
    raw = store.get_connection(site["id"], "tiktok")["_detail_raw"]
    assert raw["refresh_token"] == "rt-new"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_layer_sends_the_documented_requests():
    sent = []

    def urlopen(req, timeout=30):
        sent.append(req)
        if req.full_url == tt.TOKEN_URL:
            return _Resp(json.dumps({"access_token": "a", "refresh_token": "r",
                                     "expires_in": 7200, "refresh_expires_in": 100,
                                     "open_id": "o", "scope": "s"}).encode())
        if req.full_url == tt.DIRECT_URL:
            return _Resp(json.dumps({"data": {"publish_id": "p1"},
                                     "error": {"code": "ok", "message": ""}}).encode())
        return _Resp(json.dumps({"data": {}, "error": {"code": "spam_risk_too_many_posts",
                                                       "message": "Too many posts"}}).encode())
    env = {"TIKTOK_CLIENT_KEY": "ck", "TIKTOK_CLIENT_SECRET": "cs"}
    tok = tt.exchange_code("https://www.axionory.com", "code1", env=env, urlopen=urlopen)
    assert tok["access_token"] == "a" and tok["expires_at"] > time.time()
    body = urllib.parse.parse_qs(sent[0].data.decode())
    assert body["grant_type"] == ["authorization_code"] and body["client_key"] == ["ck"]
    assert body["redirect_uri"] == ["https://www.axionory.com/account/auth/tiktok/callback"]
    assert tt.publish("a", "https://x/v.mp4", mode="direct", post_info={"privacy_level": "SELF_ONLY"},
                      urlopen=urlopen) == "p1"
    sent_body = json.loads(sent[1].data)
    assert sent_body["source_info"] == {"source": "PULL_FROM_URL", "video_url": "https://x/v.mp4"}
    assert sent[1].get_header("Authorization") == "Bearer a"
    with pytest.raises(tt.TikTokError, match="Too many posts"):
        tt.creator_info("a", urlopen=urlopen)
    assert sent[2].get_method() == "POST"
