"""Vector only — no bitmap images anywhere on Axionory, except three
deliberate exceptions.

Every graphic on Axionory (landing, signed-in pages, admin) is
vector (inline SVG). No PNG, JPG, WebP, or GIF is rendered on any
page, with the exceptions below.

Deliberate exceptions:
- (Spencer, 2026-11-09) the website snapshot's screenshot of the
  customer's OWN site, shown only on that owner's site page and
  served by the owner-only route /websites/sites/<id>/snapshot-image.
  It is customer content, not a Axionory graphic.
- (Spencer, 2026-11-12) the Axionory logo — a supplied brand asset,
  not a generated graphic — used as the nav brand (landing header +
  signed-in sidebar) and the favicon, served from ventoryn/static/
  by ventoryn/public_files.py.
- (Spencer, 2026-11-12) the dashboard screenshot — a photo of the
  product itself (the live overview with real numbers), not a UI
  graphic — shown once on the landing page as the hero preview,
  served from ventoryn/static/ by ventoryn/public_files.py.
- (Spencer, 2026-11-13) the link-preview card /static/og-card.jpg,
  named only in the landing page's og:image / twitter:image meta tags.
  No page renders it: Facebook, X, LinkedIn and the like fetch it
  when someone shares an Axionory link.
The template scan below allows exactly those and nothing else.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from core.audit import AuditLog
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
ADMIN_PASSWORD = "test-admin-password"
BITMAP = re.compile(r"\.(png|jpg|jpeg|webp|gif)\b", re.IGNORECASE)
BRAND_IMG = re.compile(r"<img src=['\"]/static/logo\.png['\"] alt=['\"]Axionory['\"]>")
FAVICON_LINK = re.compile(
    r"<link rel=['\"]icon['\"] type=['\"]image/png['\"] href=['\"]/favicon\.png['\"]>")
OG_CARD_META = re.compile(
    r"<meta (?:property=['\"]og:image['\"]|name=['\"]twitter:image['\"]) content=['\"].*?"
    r"/static/og-card\.jpg['\"]>")
DASHBOARD_IMG = re.compile(
    r"<img class=['\"]preview-shot['\"] src=['\"]/static/dashboard-preview\.webp['\"][^>]*>")


def _app_with():
    store, config = operator_store_and_config(
        admin_password=ADMIN_PASSWORD)
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     approvals=ApprovalGate(), audit=AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store


def _signed_client(app, store):
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    store.create_site(account["id"], "https://ada.example.com")
    return client


def _admin_client(app):
    client = TestClient(app)
    r = client.post("/admin/login", data={"password": ADMIN_PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 303
    return client


def test_no_bitmap_in_any_rendered_page():
    app, store = _app_with()
    client = _signed_client(app, store)
    admin = _admin_client(app)
    # The 2FA setup page needs a session WITHOUT 2FA set up.
    from _signin import sign_in_raw
    raw = TestClient(app)
    sign_in_raw(raw, store, email="raw@example.com",
                sub="google-sub-raw", name="Raw")
    pages = [
        (TestClient(app), "/"),  # public landing page
        (client, "/ops"),
        (client, "/portfolio"),
        (client, "/approvals"),
        (client, "/report"),
        (client, "/account/dashboard"),
        (client, "/account/subscribe"),
        (client, "/websites/sites/new"),
        (client, "/account/security"),
        (raw, "/account/two-factor/setup"),
        (admin, "/admin"),
        (admin, "/admin/subscribers"),
        (admin, "/admin/accounts"),
        (admin, "/admin/agents"),
        (admin, "/admin/two-factor/setup"),
    ]
    for c, path in pages:
        r = c.get(path)
        assert r.status_code == 200, path
        text = FAVICON_LINK.sub("", BRAND_IMG.sub("", r.text))
        if path == "/":
            # The one allowed landing-page bitmap: the real dashboard
            # screenshot (Spencer, 2026-11-12).
            assert len(DASHBOARD_IMG.findall(text)) == 1, path
            text = DASHBOARD_IMG.sub("", text)
            # The link-preview card: meta tags only, never an <img>.
            assert len(OG_CARD_META.findall(text)) == 2, path
            text = OG_CARD_META.sub("", text)
        assert "<img" not in text.lower(), path
        assert not BITMAP.search(text), (path, BITMAP.search(text))
        if path != "/":
            assert "og:image" not in r.text, path


def test_no_bitmap_reference_in_page_templates():
    # The templates/routes that render Axionory pages must not
    # reference a bitmap file at all. (core/video_render.py's .png
    # intermediates for MP4s and the analyzer's reads of OTHER sites'
    # og:image are not page templates and are out of scope.)
    here = Path(__file__).resolve().parent.parent
    sources = [
        here / "ventoryn" / "app.py",
        here / "ventoryn" / "nav.py",
        here / "ventoryn" / "saas_routes.py",
        here / "ventoryn" / "site_snapshot.py",
        here / "ventoryn" / "ui.py",
        # (The game file ventoryn/static/game.html is deleted —
        # Overview is server-rendered now — so it is no longer
        # scanned.)
    ]
    snapshot_img = re.compile(
        r"<img class='v-snap-shot' src='/websites/sites/\{sid\}/snapshot-image'")
    # Expected count of each allowed exception, by file: the brand
    # logo renders once in the landing nav (app.py) and once in the
    # signed-in sidebar (nav.py); the favicon link renders once per
    # <head> — app.py's landing page has one, ui.py's shared shell
    # has two (sidebar and centered page variants).
    brand_counts = {"app.py": 1, "nav.py": 1}
    favicon_counts = {"app.py": 1, "ui.py": 2}
    dashboard_counts = {"app.py": 1}
    og_card_counts = {"app.py": 2}
    for path in sources:
        text = path.read_text(encoding="utf-8")
        if path.name == "site_snapshot.py":
            # The one allowed image: the owner's own-site screenshot.
            assert len(snapshot_img.findall(text)) == 1, path
            text = snapshot_img.sub("", text)
        if path.name in brand_counts:
            assert len(BRAND_IMG.findall(text)) == brand_counts[path.name], path
            text = BRAND_IMG.sub("", text)
        if path.name in favicon_counts:
            assert len(FAVICON_LINK.findall(text)) == favicon_counts[path.name], path
            text = FAVICON_LINK.sub("", text)
        if path.name in dashboard_counts:
            assert len(DASHBOARD_IMG.findall(text)) == dashboard_counts[path.name], path
            text = DASHBOARD_IMG.sub("", text)
        if path.name in og_card_counts:
            assert len(OG_CARD_META.findall(text)) == og_card_counts[path.name], path
            text = OG_CARD_META.sub("", text)
        assert "<img" not in text.lower(), path
        match = BITMAP.search(text)
        assert match is None, (path, match.group(0) if match else "")


def test_graphics_present_are_svg():
    # The pages do carry graphics — they are inline SVG, not bitmaps.
    app, store = _app_with()
    client = _signed_client(app, store)
    r = client.get("/ops")
    assert r.status_code == 200
    assert "<svg" in r.text  # crew icons + charts are SVG
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert "<svg" in r.text  # trust checkmarks + step icons are SVG
