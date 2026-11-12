"""Vector only — no bitmap images anywhere on Axionory.

Every graphic on Axionory (landing, signed-in pages, admin) is
vector (inline SVG). No PNG, JPG, WebP, or GIF is rendered on any
page. The og-image is intentionally ABSENT (it would be a bitmap,
used only by external link previews, never rendered on a page) —
that is Spencer's call, flagged in the report, not silently changed.

ONE deliberate exception (Spencer, 2026-11-09): the website snapshot's
screenshot of the customer's OWN site, shown only on that owner's site
page and served by the owner-only route
/websites/sites/<id>/snapshot-image. It is customer content, not a
Axionory graphic. The template scan below allows exactly that <img>
and nothing else.
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
        assert "<img" not in r.text.lower(), path
        assert not BITMAP.search(r.text), (path, BITMAP.search(r.text))
        assert "og:image" not in r.text, path  # intentionally absent


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
        # (The game file ventoryn/static/game.html is deleted —
        # Overview is server-rendered now — so it is no longer
        # scanned.)
    ]
    snapshot_img = re.compile(
        r"<img class='v-snap-shot' src='/websites/sites/\{sid\}/snapshot-image'")
    for path in sources:
        text = path.read_text(encoding="utf-8")
        if path.name == "site_snapshot.py":
            # The one allowed image: the owner's own-site screenshot.
            assert len(snapshot_img.findall(text)) == 1, path
            text = snapshot_img.sub("", text)
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
    assert "<svg" in r.text  # the dashboard preview is SVG
