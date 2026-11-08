"""Sign-in helpers for the auth-gated dashboard tests.

The dashboard now requires sign-in: /ops, /game, /portfolio,
/approvals, /report, /api/snapshot and /api/report need a session
cookie; the operator tools (/businesses, /experiments, /reports,
/experiment-001, /api/tick, /api/reset) need the operator account —
the account whose email matches SAAS_OPERATOR_EMAIL. /admin needs
the admin password (VENTORYN_ADMIN_PASSWORD), never a Google session.
Tests that need operator data sign in as the operator; tests that
need a plain customer sign in with another email.

Two-factor is FORCED: sign_in() enables TOTP on the test account
and marks the session as 2FA-verified by default, so existing tests
keep working. Gate-specific tests use sign_in_raw() for a session
that has not passed 2FA.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ventoryn.saas as saas
from ventoryn.saas import SaasConfig, SaasStore
from ventoryn.totp import code_at

OPERATOR_EMAIL = "operator@example.com"


def operator_store_and_config(email=OPERATOR_EMAIL, admin_password=""):
    """A fresh in-memory store + a config whose operator is `email`.

    `admin_password` sets VENTORYN_ADMIN_PASSWORD for tests that
    log in to /admin — empty (the default) means the admin login
    always fails, exactly like an unconfigured server."""
    store = SaasStore(":memory:")
    config = SaasConfig(operator_email=email, base_url="http://testserver",
                        admin_password=admin_password)
    return store, config


def sign_in(client, store, email=OPERATOR_EMAIL,
            sub="google-sub-op", name="Operator", totp=True):
    """Create (or reuse) the account for `email` and set its session
    cookie on `client`. Returns the account dict.

    Two-factor is mandatory: by default the test account has TOTP
    set up and this session has passed it, so the session opens
    every product page. Pass totp=False for a raw session (no 2FA
    set up, not verified) — the forced-2FA gate tests use that."""
    account = store.get_account_by_sub(sub)
    if account is None:
        account = store.create_account(sub, email, name)
    token = store.create_session(account["id"])
    if totp:
        if not store.totp_state(account["id"])["enabled"]:
            setup = store.begin_totp_setup(account["id"])
            assert store.confirm_totp_setup(
                account["id"], code_at(setup["secret"]))
        store.mark_session_totp_verified(token)
    client.cookies.set(saas.SESSION_COOKIE, token)
    return account


def sign_in_raw(client, store, email=OPERATOR_EMAIL,
                sub="google-sub-op", name="Operator"):
    """A session that has NOT passed 2FA, on an account with no 2FA
    set up — the forced-2FA gate tests use this."""
    return sign_in(client, store, email=email, sub=sub, name=name,
                   totp=False)


def fresh_totp_code(store, account_id):
    """A valid current TOTP code for the account's active secret —
    for tests that must pass a fresh code to a sensitive action
    (privacy toggle, disconnect, delete)."""
    from ventoryn.totp import code_at
    account = store.get_account(account_id)
    secret = store._decrypt_secret(account["totp_secret"])
    return code_at(secret)


def sign_in_as_operator(client, app, email=OPERATOR_EMAIL):
    """For apps built by create_dashboard_app (whose config comes
    from the environment): point the app's config at `email` as the
    operator, then sign in as that account. Returns the account."""
    app.state.saas_config.operator_email = email
    return sign_in(client, app.state.saas_store, email=email)
