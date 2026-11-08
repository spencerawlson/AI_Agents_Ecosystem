"""Sign-in helpers for the auth-gated dashboard tests.

The dashboard now requires sign-in: /ops, /game, /portfolio,
/approvals, /report, /api/snapshot and /api/report need a session
cookie; the operator tools (/businesses, /experiments, /reports,
/experiment-001, /api/tick, /api/reset) and /admin need the
operator account — the account whose email matches
SAAS_OPERATOR_EMAIL. Tests that need operator data sign in as the
operator; tests that need a plain customer sign in with another
email.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.saas as saas
from core.saas import SaasConfig, SaasStore

OPERATOR_EMAIL = "operator@example.com"


def operator_store_and_config(email=OPERATOR_EMAIL):
    """A fresh in-memory store + a config whose operator is `email`."""
    store = SaasStore(":memory:")
    config = SaasConfig(operator_email=email, base_url="http://testserver")
    return store, config


def sign_in(client, store, email=OPERATOR_EMAIL,
            sub="google-sub-op", name="Operator"):
    """Create (or reuse) the account for `email` and set its session
    cookie on `client`. Returns the account dict."""
    account = store.get_account_by_sub(sub)
    if account is None:
        account = store.create_account(sub, email, name)
    token = store.create_session(account["id"])
    client.cookies.set(saas.SESSION_COOKIE, token)
    return account


def sign_in_as_operator(client, app, email=OPERATOR_EMAIL):
    """For apps built by create_dashboard_app (whose config comes
    from the environment): point the app's config at `email` as the
    operator, then sign in as that account. Returns the account."""
    app.state.saas_config.operator_email = email
    return sign_in(client, app.state.saas_store, email=email)
