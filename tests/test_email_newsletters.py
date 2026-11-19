"""Email newsletters via Resend — adapter, draft-only guarantee,
honest unconfigured state, per-site storage.

No live provider calls anywhere: the Resend transport is a fake
patched in, and the LLM is a fake. No credential value appears in
this file — the only "key" is the obvious fake "re_test_fake_key".
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from orchestrator.approvals import ApprovalGate
from ventoryn.app import create_app
from ventoryn.saas import SaasError
import ventoryn.newsletters as newsletters
import ventoryn.saas_routes as saas_routes
from ventoryn.resend_email import MAX_RECIPIENTS, send_email

from _signin import (
    fresh_totp_code,
    operator_store_and_config,
    sign_in,
)

CUSTOMER = ("ada@example.com", "google-sub-ada", "Ada")
FAKE_KEY = "re_test_fake_key"
FROM = "news@ada.example.com"
TO = ["reader@example.com", "friend@example.com"]


@pytest.fixture(autouse=True)
def _token_key(monkeypatch):
    monkeypatch.setenv("VENTORYN_TOKEN_KEY", "test-token-key-newsletters")


# ---------------------------------------------------------------------------
# Fake Resend transport — records every call, answers canned.
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeResend:
    """An httpx-like client. `fail_with` makes every call fail."""

    def __init__(self, status=200, payload=None, fail_with=None):
        self.calls = []
        self.status = status
        self.payload = payload if payload is not None else {"id": "re_fake123"}
        self.fail_with = fail_with

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.fail_with is not None:
            return _FakeResponse(*self.fail_with)
        return _FakeResponse(self.status, self.payload)


def _app_with(resend=False):
    store, config = operator_store_and_config()
    if resend:
        config.resend_api_key = FAKE_KEY
    app = create_app(registry=BusinessRegistry(),
                     ledger=Ledger(),
                     approvals=ApprovalGate(),
                     audit=AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store, config


def _customer_client(app, store):
    email, sub, name = CUSTOMER
    client = TestClient(app)
    account = sign_in(client, store, email=email, sub=sub, name=name)
    return client, account


def _seed_site(store, account, url="https://ada.example.com"):
    return store.create_site(account["id"], url)


def _seed_draft(store, account, site, **over):
    kw = {"subject": "News from Ada", "body_text": "Hello readers.",
          "from_email": FROM, "recipients": list(TO)}
    kw.update(over)
    return store.create_newsletter(site["id"], account["id"], **kw)


# ---------------------------------------------------------------------------
# Resend adapter
# ---------------------------------------------------------------------------

def test_adapter_send_success_fake_200():
    fake = FakeResend()
    result = send_email(FAKE_KEY, from_email=FROM, to=TO,
                        subject="Hi", text="hello", client=fake)
    assert result["id"] == "re_fake123"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://api.resend.com/emails"
    assert call["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert call["json"]["from"] == FROM
    assert call["json"]["to"] == TO
    assert call["json"]["subject"] == "Hi"
    assert call["json"]["text"] == "hello"


def test_adapter_failure_surfaces_as_error():
    fake = FakeResend(fail_with=(401, {"error": {"message": "bad key"}}))
    with pytest.raises(SaasError, match="Resend did not accept"):
        send_email(FAKE_KEY, from_email=FROM, to=TO,
                   subject="Hi", text="hello", client=fake)
    assert len(fake.calls) == 1


def test_adapter_no_key_makes_no_call():
    fake = FakeResend()
    with pytest.raises(SaasError, match="not set up yet"):
        send_email("", from_email=FROM, to=TO,
                   subject="Hi", text="hello", client=fake)
    assert fake.calls == []


def test_adapter_empty_recipients_makes_no_call():
    fake = FakeResend()
    with pytest.raises(SaasError, match="no recipient"):
        send_email(FAKE_KEY, from_email=FROM, to=[],
                   subject="Hi", text="hello", client=fake)
    assert fake.calls == []


def test_adapter_over_batch_cap_refused_before_call():
    fake = FakeResend()
    too_many = [f"r{i}@example.com" for i in range(MAX_RECIPIENTS + 1)]
    with pytest.raises(SaasError, match="Too many recipients"):
        send_email(FAKE_KEY, from_email=FROM, to=too_many,
                   subject="Hi", text="hello", client=fake)
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Recipient / From validation
# ---------------------------------------------------------------------------

def test_validate_recipients_splits_dedupes_and_rejects_bad():
    assert newsletters.validate_recipients(
        "a@example.com\nb@example.com, A@example.com") == [
            "a@example.com", "b@example.com"]
    with pytest.raises(ValueError, match="not an email address"):
        newsletters.validate_recipients("not-an-email")
    with pytest.raises(ValueError, match="at least one"):
        newsletters.validate_recipients("  \n ")


def test_validate_recipients_over_cap():
    raw = "\n".join(f"r{i}@example.com" for i in range(MAX_RECIPIENTS + 1))
    with pytest.raises(ValueError, match="Too many recipients"):
        newsletters.validate_recipients(raw)


def test_validate_from_email():
    assert newsletters.validate_from_email(" news@ada.example.com ") == \
        "news@ada.example.com"
    with pytest.raises(ValueError, match="From email"):
        newsletters.validate_from_email("not-an-email")


# ---------------------------------------------------------------------------
# Generation (fake LLM + fake inventory)
# ---------------------------------------------------------------------------

class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def complete(self, *args, **kwargs):
        return {"json": self.payload}


def _fake_inventory():
    return SimpleNamespace(pages=[
        SimpleNamespace(url="https://ada.example.com/guide",
                        title="The Guide"),
        SimpleNamespace(url="https://ada.example.com/pricing",
                        title="Pricing"),
    ])


def test_draft_newsletter_from_fake_llm_and_inventory():
    llm = _FakeLLM({"subject": "This month at Ada",
                    "intro": "What changed.",
                    "sections": [
                        {"heading": "Guide",
                         "text": "Read the guide.",
                         "link": "https://ada.example.com/guide"},
                        {"heading": "Elsewhere",
                         "text": "Not our page.",
                         "link": "https://evil.example.com/x"},
                    ]})
    site = {"url": "https://ada.example.com", "country": "CA",
            "language": "en"}
    out = newsletters.draft_newsletter(site, llm=llm,
                                       inventory=_fake_inventory())
    assert out["subject"] == "This month at Ada"
    assert out["sections"][0]["link"] == "https://ada.example.com/guide"
    # A link off the site is rewritten to the home page, never kept.
    assert out["sections"][1]["link"] == "https://ada.example.com"


def test_draft_newsletter_unusable_llm_raises():
    llm = _FakeLLM({"subject": "", "intro": "", "sections": []})
    site = {"url": "https://ada.example.com"}
    with pytest.raises(RuntimeError, match="no usable newsletter"):
        newsletters.draft_newsletter(site, llm=llm,
                                     inventory=_fake_inventory())


def test_validate_newsletter_truncates_long_subject():
    out = newsletters.validate_newsletter(
        {"subject": "S" * 500, "intro": "intro",
         "sections": [{"heading": "H", "text": "T",
                       "link": "https://ada.example.com/guide"}]},
        allowed_urls={"https://ada.example.com/guide"},
        fallback_url="https://ada.example.com")
    assert out is not None and len(out["subject"]) == 200


# ---------------------------------------------------------------------------
# Draft-only guarantee + per-site storage
# ---------------------------------------------------------------------------

def test_send_unapproved_draft_refuses_and_makes_no_call():
    _app, store, config = _app_with(resend=True)
    account = store.create_account("google-sub-ada", CUSTOMER[0], "Ada")
    site = _seed_site(store, account)
    draft = _seed_draft(store, account, site)
    fake = FakeResend()
    ok, message = newsletters.send_newsletter(
        store, config, account, site["id"], draft["id"], client=fake)
    assert ok is False
    assert "still a draft" in message
    assert fake.calls == []
    assert store.get_newsletter(draft["id"])["status"] == "draft"
    assert store.list_newsletter_sends(draft["id"]) == []


def test_send_after_approval_succeeds_and_second_send_refuses():
    _app, store, config = _app_with(resend=True)
    account = store.create_account("google-sub-ada", CUSTOMER[0], "Ada")
    site = _seed_site(store, account)
    draft = _seed_draft(store, account, site)
    assert newsletters.approve_newsletter(
        store, draft["id"], decided_by=CUSTOMER[0]) is True
    fake = FakeResend()
    ok, message = newsletters.send_newsletter(
        store, config, account, site["id"], draft["id"], client=fake)
    assert ok is True
    assert "Sent to 2 recipient(s)" in message
    assert len(fake.calls) == 1
    sent = store.get_newsletter(draft["id"])
    assert sent["status"] == "sent"
    assert sent["resend_id"] == "re_fake123"
    assert sent["sent_at"]
    sends = store.list_newsletter_sends(draft["id"])
    assert len(sends) == 1 and sends[0]["status"] == "sent"
    # A second send is refused and makes no further call.
    ok, message = newsletters.send_newsletter(
        store, config, account, site["id"], draft["id"], client=fake)
    assert ok is False and "already sent" in message
    assert len(fake.calls) == 1


def test_send_failure_recorded_and_retryable():
    _app, store, config = _app_with(resend=True)
    account = store.create_account("google-sub-ada", CUSTOMER[0], "Ada")
    site = _seed_site(store, account)
    draft = _seed_draft(store, account, site)
    newsletters.approve_newsletter(store, draft["id"],
                                   decided_by=CUSTOMER[0])
    failing = FakeResend(
        fail_with=(500, {"error": {"message": "provider down"}}))
    ok, _message = newsletters.send_newsletter(
        store, config, account, site["id"], draft["id"], client=failing)
    assert ok is False
    failed = store.get_newsletter(draft["id"])
    assert failed["status"] == "send_failed"
    sends = store.list_newsletter_sends(draft["id"])
    assert len(sends) == 1 and sends[0]["status"] == "failed"
    assert "provider down" in sends[0]["error"]
    # Retry: re-approve the failed send, then it goes out.
    assert store.decide_newsletter(draft["id"], "approved",
                                   decided_by=CUSTOMER[0]) is True
    good = FakeResend()
    ok, _message = newsletters.send_newsletter(
        store, config, account, site["id"], draft["id"], client=good)
    assert ok is True
    assert store.get_newsletter(draft["id"])["status"] == "sent"


def test_send_unconfigured_refuses_and_makes_no_call():
    _app, store, config = _app_with(resend=False)
    assert config.resend_configured is False
    account = store.create_account("google-sub-ada", CUSTOMER[0], "Ada")
    site = _seed_site(store, account)
    draft = _seed_draft(store, account, site)
    newsletters.approve_newsletter(store, draft["id"],
                                   decided_by=CUSTOMER[0])
    fake = FakeResend()
    ok, message = newsletters.send_newsletter(
        store, config, account, site["id"], draft["id"], client=fake)
    assert ok is False
    assert "not set up yet" in message
    assert fake.calls == []
    assert store.get_newsletter(draft["id"])["status"] == "approved"


def test_newsletters_are_per_site():
    _app, store, _config = _app_with()
    account = store.create_account("google-sub-ada", CUSTOMER[0], "Ada")
    site_a = _seed_site(store, account, "https://a.example.com")
    site_b = _seed_site(store, account, "https://b.example.com")
    _seed_draft(store, account, site_a)
    assert len(store.list_newsletters(site_a["id"])) == 1
    assert store.list_newsletters(site_b["id"]) == []
    # Sending site A's newsletter as if it were site B's is refused.
    draft_a = store.list_newsletters(site_a["id"])[0]
    other = store.create_account("google-sub-bob", "bob@example.com", "Bob")
    ok, message = newsletters.send_newsletter(
        store, _config, other, site_b["id"], draft_a["id"])
    assert ok is False and "not found" in message


def test_approve_twice_second_is_refused():
    _app, store, _config = _app_with()
    account = store.create_account("google-sub-ada", CUSTOMER[0], "Ada")
    site = _seed_site(store, account)
    draft = _seed_draft(store, account, site)
    assert newsletters.approve_newsletter(
        store, draft["id"], decided_by=CUSTOMER[0]) is True
    assert newsletters.approve_newsletter(
        store, draft["id"], decided_by=CUSTOMER[0]) is False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_site_page_shows_honest_unconfigured_state():
    app, store, _config = _app_with(resend=False)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    resp = client.get(f"/websites/sites/{site['id']}")
    assert resp.status_code == 200
    assert "Email newsletters" in resp.text
    assert "Email sending is not set up yet" in resp.text
    assert "Resend API key" in resp.text


def test_site_page_shows_ready_state_when_configured():
    app, store, _config = _app_with(resend=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    resp = client.get(f"/websites/sites/{site['id']}")
    assert resp.status_code == 200
    assert "Email sending is ready" in resp.text


def test_compose_draft_route_creates_draft_only(monkeypatch):
    app, store, _config = _app_with(resend=False)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    resp = client.post(f"/websites/sites/{site['id']}/newsletters",
                       data={"subject": "Hello", "body_text": "Body.",
                             "from_email": FROM,
                             "recipients": "reader@example.com"},
                       follow_redirects=False)
    assert resp.status_code == 303
    drafts = store.list_newsletters(site["id"])
    assert len(drafts) == 1
    assert drafts[0]["status"] == "draft"
    assert drafts[0]["recipients"] == ["reader@example.com"]


def test_compose_with_bad_recipient_stores_nothing():
    app, store, _config = _app_with(resend=False)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    resp = client.post(f"/websites/sites/{site['id']}/newsletters",
                       data={"subject": "Hello", "body_text": "Body.",
                             "from_email": FROM,
                             "recipients": "not-an-email"})
    assert resp.status_code == 400
    assert store.list_newsletters(site["id"]) == []


def test_generate_route_uses_fake_llm(monkeypatch):
    app, store, _config = _app_with(resend=False)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    fake_llm = _FakeLLM({"subject": "Generated", "intro": "Intro.",
                         "sections": [{"heading": "H", "text": "T",
                                       "link": "https://ada.example.com"}]})
    monkeypatch.setattr(
        saas_routes, "draft_newsletter",
        lambda site, **kw: newsletters.draft_newsletter(
            site, llm=fake_llm, inventory=_fake_inventory()))
    resp = client.post(f"/websites/sites/{site['id']}/newsletters/generate",
                       data={"from_email": FROM,
                             "recipients": "reader@example.com"},
                       follow_redirects=False)
    assert resp.status_code == 303
    drafts = store.list_newsletters(site["id"])
    assert len(drafts) == 1 and drafts[0]["subject"] == "Generated"
    assert drafts[0]["sections"][0]["heading"] == "H"


def test_approve_route_unconfigured_is_honest_and_sends_nothing():
    app, store, _config = _app_with(resend=False)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    draft = _seed_draft(store, account, site)
    resp = client.post(
        f"/websites/sites/{site['id']}/newsletters/{draft['id']}/approve",
        data={"totp_code": fresh_totp_code(store, account["id"])})
    assert resp.status_code == 400
    assert "not set up yet" in resp.text
    assert "Nothing was sent" in resp.text
    # The approval was recorded, the send was not.
    assert store.get_newsletter(draft["id"])["status"] == "approved"
    assert store.list_newsletter_sends(draft["id"]) == []


def test_approve_route_sends_when_configured(monkeypatch):
    app, store, _config = _app_with(resend=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    draft = _seed_draft(store, account, site)
    fake = FakeResend()
    monkeypatch.setattr(newsletters, "send_email",
                        lambda *a, **k: _send_via_fake(fake, *a, **k))
    resp = client.post(
        f"/websites/sites/{site['id']}/newsletters/{draft['id']}/approve",
        data={"totp_code": fresh_totp_code(store, account["id"])})
    assert resp.status_code == 200
    assert "Sent to 2 recipient(s)" in resp.text
    assert len(fake.calls) == 1
    assert store.get_newsletter(draft["id"])["status"] == "sent"


def _send_via_fake(fake, api_key, *, from_email, to, subject,
                   html=None, text=None, client=None):
    """Run the real adapter's validation against the fake transport."""
    from ventoryn.resend_email import send_email as real_send
    return real_send(api_key, from_email=from_email, to=to,
                     subject=subject, html=html, text=text, client=fake)


def test_approve_route_wrong_account_is_404():
    app, store, _config = _app_with(resend=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    draft = _seed_draft(store, account, site)
    other_client = TestClient(app)
    other = sign_in(other_client, store, email="bob@example.com",
                    sub="google-sub-bob", name="Bob")
    assert other["id"] != account["id"]
    resp = other_client.post(
        f"/websites/sites/{site['id']}/newsletters/{draft['id']}/approve",
        data={"totp_code": fresh_totp_code(store, other["id"])})
    assert resp.status_code == 404
    assert store.get_newsletter(draft["id"])["status"] == "draft"


def test_reject_route_marks_rejected():
    app, store, _config = _app_with(resend=False)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    draft = _seed_draft(store, account, site)
    resp = client.post(
        f"/websites/sites/{site['id']}/newsletters/{draft['id']}/reject",
        follow_redirects=False)
    assert resp.status_code == 303
    assert store.get_newsletter(draft["id"])["status"] == "rejected"


def test_approvals_queue_lists_pending_newsletter():
    app, store, _config = _app_with(resend=False)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    _seed_draft(store, account, site)
    resp = client.get("/approvals")
    assert resp.status_code == 200
    assert "Email newsletter" in resp.text
    assert "News from Ada" in resp.text
    assert "Approve &amp; send" in resp.text
