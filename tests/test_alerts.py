"""Alerts: on /alerts and in the bell's panel, not inline on pages."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_saas_shell import _login, client, config, store  # noqa: F401
from ventoryn import campaigns


def test_stripe_hints_live_in_alerts_not_on_integrations(client, store):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://shop.example.com")
    integ = client.get("/integrations").text
    assert "<div class='v-notice v-notice-warn'>" not in integ  # no banner wall
    main = integ.split("<main", 1)[-1]
    assert "Connect Stripe sales for" not in main  # only in the bell's panel
    page = client.get("/alerts").text
    assert "Connect Stripe sales for shop.example.com" in page
    assert f"href='/alerts/open?key=stripe%3A{site['id']}'" in page  # opens + marks read


def test_bell_shows_count_and_panel_on_every_page(client, store):
    acct = _login(client, store)
    store.create_site(acct["id"], "https://shop.example.com")
    store.create_site(acct["id"], "https://blog.example.org")
    for path in ("/ops", "/portfolio", "/integrations", "/account/dashboard"):
        html = client.get(path).text
        assert "id='bell-btn' href='/alerts' aria-label='Alerts (2)'" in html, path
        assert "<b class='header-badge'>2</b>" in html, path
        assert "<div class='bell-panel' id='bell-panel' hidden><div class='bell-head'><h4>Alerts</h4>" in html, path
        assert "Mark all as read" in html and "Mark as read" in html, path
        assert "See all alerts" in html, path


def test_campaign_proposals_and_held_posts_are_alerts(client, store):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://shop.example.com")
    store.update_connection(site["id"], "stripe_sales", "connected", "ok", {"webhook_secret": "w"})
    posts = [{"text": "Hello", "link": site["url"]}]
    c = campaigns.propose(store, site, posts, days=1, posts_per_day=1, with_link=True,
                          source="manual")
    assert "1 campaign proposal waiting for your approval." in client.get("/alerts").text
    campaigns.schedule(store, c)
    d = store.campaign_drafts(c["id"])[0]
    store.hold_draft(d["id"], "X is not connected for this site.")
    page = client.get("/alerts").text
    assert "1 X post for shop.example.com could not be posted: X is not connected" in page


def test_nothing_to_do(client, store):
    _login(client, store)
    html = client.get("/alerts").text
    assert "Nothing needs your attention right now." in html
    assert "aria-label='Alerts (0)'" in html and "header-badge" not in html.split("bell-btn")[1][:200]


def test_mark_as_read_lowers_the_badge(client, store):
    acct = _login(client, store)
    a = store.create_site(acct["id"], "https://shop.example.com")
    store.create_site(acct["id"], "https://blog.example.org")
    assert "aria-label='Alerts (2)'" in client.get("/ops").text
    r = client.post("/alerts/read", data={"key": f"stripe:{a['id']}"},
                    headers={"Accept": "application/json"})
    assert r.json() == {"unread": 1}
    html = client.get("/ops").text
    assert "aria-label='Alerts (1)'" in html and "<b class='header-badge'>1</b>" in html
    page = client.get("/alerts").text
    assert "1 unread." in page and "is-read" in page  # read one stays, dimmed


def test_opening_an_alert_marks_it_read_and_goes_to_its_link(client, store):
    acct = _login(client, store)
    a = store.create_site(acct["id"], "https://shop.example.com")
    r = client.get(f"/alerts/open?key=stripe:{a['id']}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/integrations/stripe_sales?site={a['id']}"
    assert "aria-label='Alerts (0)'" in client.get("/ops").text
    # An unknown key never redirects anywhere but /alerts.
    r = client.get("/alerts/open?key=https://evil.example", follow_redirects=False)
    assert r.headers["location"] == "/alerts"


def test_mark_all_and_new_state_is_unread_again(client, store):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://shop.example.com")
    store.update_connection(site["id"], "stripe_sales", "connected", "ok", {"webhook_secret": "w"})
    campaigns.propose(store, site, [{"text": "a", "link": site["url"]}], days=1,
                      posts_per_day=1, with_link=True, source="manual")
    client.post("/alerts/read-all")
    assert "aria-label='Alerts (0)'" in client.get("/ops").text
    campaigns.propose(store, site, [{"text": "b", "link": site["url"]}], days=1,
                      posts_per_day=1, with_link=True, source="manual")
    assert "aria-label='Alerts (1)'" in client.get("/ops").text  # 2 proposals now: new alert


def test_cannot_mark_someone_elses_alerts(client, store):
    _login(client, store)
    other = store.create_account("s2", "bo@example.com", "Bo")
    site = store.create_site(other["id"], "https://blog.example.org")
    client.post("/alerts/read", data={"key": f"stripe:{site['id']}"})
    assert f"stripe:{site['id']}" not in store.read_alert_keys(other["id"])
