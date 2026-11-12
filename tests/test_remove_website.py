"""Remove a website: typed confirmation + fresh 2FA, tokens revoked, data gone."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ventoryn.saas_routes as routes
from test_saas_shell import _account, _login, client, config, store  # noqa: F401
from ventoryn import campaigns


def _filled_site(store, acct, url="https://shop.example.com"):
    site = store.create_site(acct["id"], url)
    store.update_connection(site["id"], "x", "connected", "ok", {"access_token": "tok-x"})
    store.record_conversion(site["id"], 1900, "usd", "manual")
    campaigns.propose(store, site, [{"text": "hi", "link": site["url"]}], days=1,
                      posts_per_day=1, with_link=True, source="manual")
    store.record_media_usage(acct["id"], site["id"], "heygen", 30, "seconds", 1.15)
    job = store.charge_credits(acct["id"], "analyze", site["id"], is_operator=True)
    store.set_job_result(job["id"], "done", "ok")
    return site


def test_remove_website_deletes_its_data_and_revokes_tokens(client, store, monkeypatch):
    from _signin import fresh_totp_code

    revoked = []
    monkeypatch.setattr(routes, "revoke_provider_token",
                        lambda kind, raw, *a, **k: revoked.append((kind, raw.get("access_token"))))
    acct = _login(client, store)
    site = _filled_site(store, acct)
    keep = _filled_site(store, acct, "https://blog.example.org")
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "Remove this website" in html and f"Type <strong>{site['host']}</strong>" in html
    r = client.post(f"/websites/sites/{site['id']}/delete",
                    data={"confirm": "SHOP.example.com",
                          "totp_code": fresh_totp_code(store, acct["id"])},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/portfolio"
    assert store.get_site(site["id"]) is None
    assert ("x", "tok-x") in revoked
    assert store.list_jobs(acct["id"], site_id=site["id"]) == []
    assert store.open_campaign_for_site(site["id"]) is None
    assert store.media_spend_usd(site["id"]) == 0
    assert store.get_site(keep["id"]) is not None          # the other website is untouched
    assert store.media_spend_usd(keep["id"]) == 1.15


def test_needs_the_exact_address_and_a_fresh_code(client, store):
    from _signin import fresh_totp_code

    acct = _login(client, store)
    site = _filled_site(store, acct)
    r = client.post(f"/websites/sites/{site['id']}/delete",
                    data={"confirm": "wrong.example", "totp_code": fresh_totp_code(store, acct["id"])})
    assert r.status_code == 400 and store.get_site(site["id"]) is not None
    r = client.post(f"/websites/sites/{site['id']}/delete",
                    data={"confirm": site["host"], "totp_code": "000000"})
    assert r.status_code == 400 and store.get_site(site["id"]) is not None


def test_cannot_remove_someone_elses_website(client, store):
    _login(client, store)
    other = _account(store, sub="s2", email="bo@example.com")
    site = store.create_site(other["id"], "https://blog.example.org")
    r = client.post(f"/websites/sites/{site['id']}/delete", data={"confirm": site["host"]})
    assert r.status_code == 404 and store.get_site(site["id"]) is not None
