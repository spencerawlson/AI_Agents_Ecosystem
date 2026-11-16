"""Remediation R-04 (cookie consent banner), R-10 (favicon.ico) and
R-11 (one clear CTA per homepage) for axionory.com — one PR.

R-04: the landing page shows a consent banner + preference control
before any non-essential tracker loads. The first-party analytics
tag (/v.js) is injected only after the visitor accepts; declining
stores the choice and loads nothing. The stored choice is honoured
on the next visit and "Cookie settings" in the footer re-opens the
banner. (No live-browser render is available in this repo's tests —
the banner is verified by DOM inspection here; visual confirmation
is a browser step.)
R-10: a real /favicon.ico (16/32/48, converted from the brand
favicon.png) so the default request does not 404.
R-11: the waitlist is the homepage's one hero action. The hero's
single btn-primary links to /founding-customer-waitlist; every other
former primary/dark CTA is demoted to btn-secondary (light sections)
or btn-ghost (dark header/hero). The signed-in "Continue to Live
Ops" banner is the one exception — it only renders for signed-in
visitors, not on the public homepage.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from test_landing import _app_with


def _landing():
    app, _store = _app_with()
    return TestClient(app).get("/")


# ---------------------------------------------------------------------------
# R-04 — cookie consent banner
# ---------------------------------------------------------------------------

def test_cookie_banner_present_with_preference_control():
    r = _landing()
    assert r.status_code == 200
    assert 'id="cookie-consent"' in r.text
    assert 'role="dialog"' in r.text and 'aria-label="Cookie consent"' in r.text
    assert 'id="cookie-accept"' in r.text and "Accept analytics" in r.text
    assert 'id="cookie-decline"' in r.text and "Decline" in r.text
    # Preference control: the footer re-opens the banner.
    assert 'id="cookie-settings"' in r.text and "Cookie settings" in r.text
    # The banner explains what it is for and links the privacy page.
    assert "first-party analytics" in r.text
    assert 'href="/privacy"' in r.text


def test_no_tracker_loads_before_consent():
    # The raw analytics tag must NOT be in the served HTML at all —
    # the tag is created by JS only after Accept. The loader carries
    # only the site id (in <head>) for that JS to use.
    r = _landing()
    assert 'src="/v.js"' not in r.text
    assert 'data-site="8e7c68c8885a1ef3e072a01c"' not in r.text
    assert ('<script>window.AXIONORY_ANALYTICS_SITE='
            '"8e7c68c8885a1ef3e072a01c";</script>' in r.text)
    assert r.text.index("AXIONORY_ANALYTICS_SITE") < r.text.index("</head>")
    # The consent loader itself is present and consent-gated.
    assert "localStorage.getItem(KEY)" in r.text
    assert "localStorage.setItem(KEY" in r.text


def test_consent_loader_off_when_analytics_off(monkeypatch):
    monkeypatch.setenv("VENTORYN_OWN_SITE_ID", "off")
    r = _landing()
    assert "AXIONORY_ANALYTICS_SITE" not in r.text.split("</head>")[0]
    # The banner itself still renders: the preference is recorded for
    # when analytics is configured.
    assert 'id="cookie-consent"' in r.text


# ---------------------------------------------------------------------------
# R-10 — /favicon.ico serves a real icon
# ---------------------------------------------------------------------------

def test_favicon_ico_serves_real_icon():
    app, _store = _app_with()
    c = TestClient(app)
    r = c.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/x-icon"
    assert r.content[:4] == b"\x00\x00\x01\x00"  # ICO magic
    assert len(r.content) > 1000  # multi-size icon, not a stub
    # The .png still serves, and the landing page declares both.
    assert c.get("/favicon.png").status_code == 200
    page = c.get("/").text
    assert 'href="/favicon.ico"' in page
    assert 'href="/favicon.png"' in page


# ---------------------------------------------------------------------------
# R-11 — one clear CTA per homepage
# ---------------------------------------------------------------------------

def _cta_inventory():
    """(primary buttons, dark buttons) by class attribute on the
    signed-out homepage — the CSS definitions do not count."""
    text = _landing().text
    return text.count("btn btn-primary"), text.count("btn btn-dark")


def test_one_dominant_cta_on_homepage():
    primary, dark = _cta_inventory()
    assert primary == 1, f"expected exactly one btn-primary, got {primary}"
    assert dark == 0, f"expected no btn-dark CTAs, got {dark}"
    r = _landing()
    assert ('<a class="btn btn-primary" href="/founding-customer-waitlist">'
            "Join the Founding Customer Waitlist</a>" in r.text)


def test_demoted_ctas_keep_their_labels_as_secondary():
    r = _landing()
    # Same destinations, secondary styling — nothing removed, only
    # demoted, so no conversion path breaks.
    assert "Get Started" in r.text
    assert '<a class="btn btn-ghost" href="#start">Get Started</a>' in r.text
    assert "Choose Starter" in r.text and "Choose Growth" in r.text
    assert "Choose Pro" in r.text
    assert '<a class="btn btn-secondary" href="/account/subscribe?plan=starter">' in r.text
    assert "Explore the Platform" in r.text
    assert "Analyze my site — free" in r.text
    assert ('<button type="submit" class="btn btn-secondary"' in r.text)
    # The waitlist repeats below the hero, but only as secondary.
    assert r.text.count("Join the Founding Customer Waitlist") == 3
    assert (r.text.count('class="btn btn-secondary" '
                         'href="/founding-customer-waitlist"') == 2)
