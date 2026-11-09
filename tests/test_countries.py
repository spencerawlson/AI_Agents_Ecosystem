"""The market country covers every country in the world (ISO 3166-1).

- The selector lists all 249 ISO 3166-1 alpha-2 codes with their
  names, defaulting to ca, everywhere the market country appears:
  the landing-page URL box, the /ops analyze panel, and the
  /account site-add form.
- Server-side validation accepts any valid ISO code (ht, br, ng,
  jp, fr, us, ...) and rejects anything else.
- The language field is unchanged.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from core.audit import AuditLog
from core.countries import (
    COUNTRIES,
    COUNTRY_NAMES,
    DEFAULT_COUNTRY,
    country_options,
    is_valid_country,
    normalize_country,
)
from core.ledger import Ledger
from core.registry import BusinessRegistry
from ventoryn.saas import SaasConfig, SaasStore
from ventoryn.app import create_app
from orchestrator.approvals import ApprovalGate

from _signin import (
    OPERATOR_EMAIL,
    operator_store_and_config,
    sign_in,
)

CUSTOMER_A = ("ada@example.com", "google-sub-ada", "Ada")


def _app_with():
    store, config = operator_store_and_config()
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     approvals=ApprovalGate(), audit=AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store


def test_country_list_covers_all_countries():
    # ISO 3166-1 alpha-2: 249 assigned codes.
    assert len(COUNTRIES) == 249
    codes = [code for code, _name in COUNTRIES]
    assert len(set(codes)) == 249
    assert codes == sorted(codes)
    assert all(len(code) == 2 and code.islower() for code in codes)
    for code in ("ca", "ht", "br", "ng", "jp", "fr", "us", "gb",
                 "de", "in", "ke", "mx", "za"):
        assert is_valid_country(code), code
        assert COUNTRY_NAMES[code], code
    assert DEFAULT_COUNTRY == "ca"


def test_normalize_country_accepts_any_valid_code():
    assert normalize_country("HT") == "ht"
    assert normalize_country(" jp ") == "jp"
    assert normalize_country("") == "ca"
    assert normalize_country(None) == "ca"
    for bad in ("xx", "usa", "c", "123", "FRANCE"):
        try:
            normalize_country(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")


def test_country_options_renders_all_with_ca_selected():
    html = country_options("ca")
    assert html.count("<option") == 249
    assert "<option value='ca' selected>" in html
    assert "Canada (ca)" in html
    assert "Haiti (ht)" in html
    assert "Brazil (br)" in html
    assert "Nigeria (ng)" in html
    assert "Japan (jp)" in html
    html_ht = country_options("ht")
    assert "<option value='ht' selected>" in html_ht
    assert "<option value='ca' selected>" not in html_ht


def test_landing_and_ops_and_site_add_carry_the_full_selector():
    app, store = _app_with()
    # The public landing page: the URL box country selector.
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert r.text.count("<option") >= 249
    assert "<option value='ca' selected>" in r.text
    # The /ops analyze panel: same selector, ca selected.
    client = TestClient(app)
    sign_in(client, store)
    r = client.get("/ops")
    assert r.status_code == 200
    assert "<option value='ca' selected>" in r.text
    assert "Haiti (ht)" in r.text
    # The site-add form: same selector.
    r = client.get("/account/sites/new")
    assert r.status_code == 200
    assert "<option value='ca' selected>" in r.text
    assert "Japan (jp)" in r.text
    # The language field is unchanged (en/fr/ht).
    assert "English (en)" in r.text
    assert "French (fr)" in r.text
    assert "Haitian Creole (ht)" in r.text


def test_analyze_accepts_a_spread_of_codes_end_to_end():
    # Signed in on purpose: the anonymous preview is rate-limited
    # to 5 per IP per hour, and this test starts six — signing in
    # exercises the same country validation without the limit.
    app, store = _app_with()
    # ...and not about the cap on concurrent analyses either.
    app.state.analyze_max_running = 100
    client = TestClient(app)
    sign_in(client, store)
    for code in ("ht", "br", "ng", "jp", "fr", "us"):
        r = client.post("/api/analyze",
                        json={"url": "https://example.com",
                              "country": code})
        assert r.status_code == 202, (code, r.text)
        assert r.json()["job_id"]
    for bad in ("xx", "usa", "123"):
        r = client.post("/api/analyze",
                        json={"url": "https://example.com",
                              "country": bad})
        assert r.status_code == 400, (bad, r.text)


def test_site_add_accepts_any_valid_code_and_rejects_invalid():
    app, store = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    for code, url in (("jp", "https://jp.example.com"),
                      ("ht", "https://ht.example.com"),
                      ("ng", "https://ng.example.com")):
        r = client.post("/account/sites",
                        data={"url": url, "country": code,
                              "language": "en"},
                        follow_redirects=False)
        assert r.status_code == 303, (code, r.text)
    sites = store.list_sites(account["id"])
    assert {s["country"] for s in sites} == {"jp", "ht", "ng"}
    r = client.post("/account/sites",
                    data={"url": "https://bad.example.com",
                          "country": "xx", "language": "en"})
    assert r.status_code == 400
    assert "xx" in r.text
