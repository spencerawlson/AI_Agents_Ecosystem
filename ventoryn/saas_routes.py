"""Customer-facing SaaS routes for Ventoryn (/account/...).

Registered by ventoryn.app.create_app() — the shell lives on the same
dashboard process, so a customer signs in and manages their sites next
to the live-ops view. Every page is server-rendered HTML, mobile-first
(single column on phones, 44px+ tap targets), light styling.

Pages:
  GET  /account                         -> Live Ops, or the landing page
  GET  /account/login                   -> the landing page (sign-in
                                          lives there, visible on it)
  GET  /account/auth/google/start       -> Google authorization URL
  GET  /account/auth/google/callback    code -> session cookie -> /ops
  GET  /account/logout                  end the session -> landing page
  GET  /account/dashboard               "My account": plan, credits,
                                         sites, jobs, funnel
  GET  /account/subscribe               the $20/month plan
  POST /account/subscribe               create a Stripe checkout session
  POST /account/billing/webhook         Stripe webhook (signature-verified)
  GET  /websites/sites/new               add a website
  POST /websites/sites                   create the site
  GET  /websites/sites/{site_id}         site detail: funnel, connections, jobs
  POST /websites/sites/{site_id}/connections/{kind}
                                         connect Plausible / Stripe sales
  POST /websites/sites/{site_id}/jobs    run a job (analyze today)
  POST /websites/sites/{site_id}/conversions
                                         owner-recorded sale (fallback)
  GET  /account/two-factor/setup        forced TOTP setup (QR + code)
  POST /account/two-factor/setup        confirm setup, backup codes shown
  GET  /account/two-factor/verify       code at every sign-in
  POST /account/two-factor/verify       code or backup code -> session
  GET  /account/security                2FA status, pause posting, delete
  POST /account/security/pause          pause / resume all posting
  POST /account/delete                  revoke tokens, delete everything
  POST /websites/sites/{site_id}/privacy revenue/count/details toggles
  POST /websites/sites/{site_id}/connections/{kind}/disconnect
                                         revoke at provider, then delete
  GET  /websites/sites/{site_id}/connections/x/start
                                         X OAuth 2.0 + PKCE authorize redirect
  GET  /account/auth/x/callback         code -> tokens -> connection stored
  POST /websites/sites/{site_id}/social/x/drafts
                                         compose a draft (never posts it)
  POST /websites/sites/{site_id}/social/x/drafts/{draft_id}/approve
                                         the ONLY path that posts to X
  POST /websites/sites/{site_id}/social/x/drafts/{draft_id}/reject
  GET  /websites/sites/{site_id}/connections/facebook/start
                                         Meta OAuth authorize redirect
  GET  /account/auth/facebook/callback  code -> tokens -> Page picker
  POST /account/auth/facebook/page      pick a Page -> Page token stored
  GET  /websites/sites/{site_id}/connections/instagram/start
                                         Meta OAuth authorize redirect
  GET  /account/auth/instagram/callback code -> tokens -> Business account
  POST /websites/sites/{site_id}/social/facebook/drafts
                                         compose a draft (never posts it)
  POST /websites/sites/{site_id}/social/facebook/drafts/{draft_id}/approve
                                         the ONLY path that posts to Facebook
  POST /websites/sites/{site_id}/social/facebook/drafts/{draft_id}/reject
  POST /websites/sites/{site_id}/social/instagram/drafts
                                         compose a draft (never posts it)
  POST /websites/sites/{site_id}/social/instagram/drafts/{draft_id}/approve
                                         the ONLY path that posts to Instagram
  POST /websites/sites/{site_id}/social/instagram/drafts/{draft_id}/reject

Sign-in flow (the ShipItAyiti flow): the landing page carries the
sign-in and the URL box; after Google sign-in EVERYONE goes straight
to Live Ops (/ops), scoped to their account. A website typed on the
landing page rides along (?site_url=…) so /ops can offer the
subscribe / add-site step for it. /admin is a separate URL for the
operator only (see ventoryn.app).

A job that costs more credits than the balance is refused with the
balance shown. The only routes that ever publish anything external
are the X / Facebook / Instagram draft-approve routes above —
composing a draft never posts it, and nothing else here posts or
publishes anywhere.
"""

from __future__ import annotations

import html
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import File, Form, HTTPException, Request, UploadFile

from ventoryn import site_jobs
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.countries import country_options
from ventoryn.qrcode import qr_svg
from ventoryn.campaign_cost import (
    MAX_DAYS, MAX_POSTS_PER_DAY, X_RATE_LINKED_POST_USD,
    X_RATE_PLAIN_POST_USD, X_RATE_SOURCE, estimate_x_campaign,
    x_post_cost_usd)
from ventoryn.campaign_cost import describe as describe_estimate
from ventoryn.first100 import first100_summary
from ventoryn.signature import sign_agent_link
from ventoryn.social_costs import caps_allow, estimate_cost, rate_note
from ventoryn.social_oauth import (
    build_authorize_url,
    exchange_code,
    exchange_for_page_token,
    exchange_long_lived_token,
    find_instagram_account,
    list_facebook_pages,
    refresh_access_token_if_needed,
)
from ventoryn.social_providers import (
    FacebookClient,
    InstagramClient,
    ProviderAmbiguousError,
    ProviderError,
)
from ventoryn.saas import (
    job_cost_label,
    APP_REGISTRATION_KINDS,
    CONNECTION_CONSENT,
    CONNECTION_REVOKE_NOTE,
    CONNECTION_SCOPES,
    CREDIT_COSTS,
    PLAN_NAME,
    PLAN_PRICE_LABEL,
    RUNNABLE_JOB_TYPES,
    SESSION_COOKIE,
    SESSION_TTL_DAYS,
    InsufficientCredits,
    SaasConfig,
    SaasError,
    SaasStore,
    check_posting_allowed,
    exchange_google_code,
    exchange_x_code,
    google_auth_url,
    google_userinfo,
    handle_customer_sale_webhook,
    handle_platform_webhook,
    pkce_pair,
    plausible_visitors,
    post_to_x,
    revoke_provider_token,
    slug_for_url,
    validate_site_url,
    verify_webhook_signature,
    create_checkout_session,
    create_wallet_topup_session,
    x_account_summary,
    x_auth_url,
    x_userinfo,
)
from ventoryn.totp import provisioning_uri

#: Where analyze reports are written (reports/analyze_<host>_<date>.md,
#: same as the CLI). Tests override this with a temp dir.
REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"

#: Short-lived cookie holding the website URL a visitor typed on the
#: landing page while they sign in and subscribe. It is the only
#: carrier across the Google round-trip (the OAuth state is a random
#: CSRF token and holds nothing else). 30 minutes, HttpOnly, same-site
#: lax; the callback consumes it and the subscribe page carries the
#: URL onward in its form, so the visitor never retypes it.
PENDING_SITE_COOKIE = "ventoryn_pending_site"
PENDING_SITE_TTL_SECONDS = 30 * 60

# Account pages share the platform shell (ventoryn/ui.py); only the
# 2FA bits below are specific to them.
_SAAS_CSS = """
.qr { background: #fff; border: 1px solid var(--v-line); border-radius: 12px;
      padding: 12px; display: inline-block; margin: 12px 0; }
.codes { font-family: ui-monospace, Consolas, monospace; font-size: 17px;
         line-height: 1.9; background: #faf9fe; border: 1px solid var(--v-line);
         border-radius: 10px; padding: 12px 16px; }
.v-main { max-width: 820px; }
/* The My account sub-nav (My account · Security · Subscribe):
   small, plain, the current page a non-clickable span — like the
   sidebar. Separators are CSS, not characters. */
.v-subnav { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 4px 0 18px;
  padding-bottom: 12px; border-bottom: 1px solid var(--v-line); }
.v-subnav-item { font-weight: 600; color: var(--v-violet-deep);
  text-decoration: none; min-height: 44px; display: inline-flex;
  align-items: center; }
.v-subnav-item.active { color: var(--v-ink); cursor: default;
  font-weight: 800; }
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, inner: str, account: dict | None = None,
          current: str | None = None, is_operator: bool = False,
          sidebar: bool = False, verified: int = 0,
          pending_approvals: int = 0, alerts: list | None = None) -> str:
    """Shell for the shell's own pages: the platform shell (purple
    sidebar + app header) whenever an account is given, else the
    shared centered page (no navigation for unsigned error pages —
    the menu belongs to signed-in pages only). Mobile-first, the
    same light styling as the Live Ops page. The menu never links
    Experiments, Experiment 001, Daily plan, Attribution, or "for
    website owners" — those stay reachable by direct URL only.

    ``verified`` / ``pending_approvals`` are computed by
    ``_signed_page`` (which is where the store and the app live —
    this function is module-level, so neither is in scope here) and
    feed the sidebar's Growth Plan card and Approvals badge. The
    sidebar ``current`` maps from the old ``current`` arg where
    sensible ("account" / "subscribe" -> "my-account"); anything
    else gets the sidebar with nothing active. The ``sidebar``
    parameter is kept for callers that pass it; every account-bearing
    page gets the sidebar now."""
    from ventoryn import ui

    if account is None:
        return ui.page(title, f"<h1>{esc(title)}</h1>{inner}",
                       extra_css=_SAAS_CSS)
    # Any signed-in account page gets the platform shell — the old
    # hamburger header is retired. ``verified`` / ``pending_approvals``
    # come from _signed_page.
    from ventoryn.nav import platform_head_html, sidebar_html

    platform_current = {"account": "my-account",
                        "subscribe": "my-account",
                        "websites": "websites:sub"}.get(current)
    sb = sidebar_html(platform_current, is_operator,
                      verified=verified,
                      pending_approvals=pending_approvals,
                      account_email=account.get("email", ""))
    head = platform_head_html(title,
                              pending_approvals=pending_approvals,
                              alerts=alerts)
    return ui.page(title, inner, sidebar=sb, page_head=head,
                   extra_css=_SAAS_CSS)


def _account_subnav_html(active: str) -> str:
    """The My account sub-nav: My account · Security · Subscribe.
    The current page is a non-clickable span with
    aria-current="page", like the sidebar — never a dead link."""
    items = (("dashboard", "My account", "/account/dashboard"),
             ("security", "Security", "/account/security"),
             ("subscribe", "Subscribe", "/account/subscribe"))
    parts = []
    for key, label, href in items:
        if key == active:
            parts.append(f"<span class='v-subnav-item active' "
                         f"aria-current='page'>{label}</span>")
        else:
            parts.append(f"<a class='v-subnav-item' href='{href}'>"
                         f"{label}</a>")
    return (f"<nav class='v-subnav' aria-label='Account'>"
            f"{''.join(parts)}</nav>")


def _credit_table() -> str:
    rows = "".join(
        f"<tr><td>{esc(name.replace('_', ' '))}</td>"
        f"<td>{cost} credits</td>"
        f"<td>{'Available now' if name in RUNNABLE_JOB_TYPES else 'Coming soon'}</td></tr>"
        for name, cost in CREDIT_COSTS.items())
    return (f"<div class='table-wrap'><table><tr><th>Agent work</th>"
            f"<th>Cost</th><th>Status</th></tr>{rows}</table></div>")


def _funnel_rows(store: SaasStore, site: dict,
                 plausible_key: str | None) -> str:
    """Visitors / signups / paying customers / revenue — each from a
    real source or the word 'unavailable'. The revenue and count
    display settings are respected separately (revenue_for_site):
    OFF hides the figure — it never shows a number the customer
    chose to hide. Nothing here is invented."""
    from ventoryn.analytics import funnel_cells

    # Ventoryn Analytics first, Plausible as the fallback (plausible_key
    # is kept in the signature for callers; funnel_cells reads the
    # connection itself).
    visitors_html, signups_html = funnel_cells(store, site,
                                               plausible_visitors)
    connections = {c["kind"]: c for c in store.get_connections(site["id"])}
    stripe = connections.get("stripe_sales") or {}
    stripe_connected = stripe.get("status") == "connected"
    rev = store.revenue_for_site(site["id"])
    if rev is None:
        paying_html = "<span class='unavailable'>unavailable</span>"
        revenue_html = "<span class='unavailable'>unavailable</span>"
    else:
        if rev["paying_customers"] is None:
            paying_html = ("<span class='unavailable'>hidden — the "
                           "customer count is not shown (change it in "
                           "Data &amp; privacy controls)</span>")
        else:
            paying_html = f"{rev['paying_customers']:,}"
        if not stripe_connected:
            revenue_html = ("<span class='unavailable'>unavailable — "
                            "connect your Stripe webhook below</span>")
        elif rev["revenue"] is None:
            revenue_html = ("<span class='unavailable'>hidden — revenue "
                            "is private by default (change it in Data "
                            "&amp; privacy controls)</span>")
        else:
            revenue_html = (f"${rev['revenue']['total_cents'] / 100:,.2f} "
                            f"{rev['revenue']['currency'].upper()}")
    return (
        f"<tr><td>Visitors (last 30 days)</td><td>{visitors_html}</td></tr>"
        f"<tr><td>Signups</td><td>{signups_html}</td></tr>"
        f"<tr><td>Paying customers</td><td>{paying_html}</td></tr>"
        f"<tr><td>Revenue</td><td>{revenue_html}</td></tr>")


def _integrations_return(value: str | None) -> str | None:
    """Where an Integrations window sends the owner back to: only a
    same-site /integrations/... path — never an absolute or
    protocol-relative URL (no open redirect)."""
    if (not value or not value.startswith("/integrations/")
            or value.startswith("//") or "\\" in value
            or len(value) > 200 or any(c in value for c in "\r\n")):
        return None
    return value


X_RETURN_COOKIE = "ventoryn_x_return"


def _save_social_tokens(store: SaasStore, site_id: str, kind: str,
                        tokens: dict, note: str = "") -> None:
    """Persist OAuth tokens on a connection (the slice's
    save_connection_tokens, inlined here because routes use
    store.update_connection directly, like main's X callback).
    access_token / refresh_token are ENCRYPTED at rest by
    update_connection. `expires_at` is stored alongside (now +
    expires_in) so the publish path can refresh a token before it
    lapses; `page_id` / `ig_user_id` ride through when the tokens
    carry them."""
    expires_at = ""
    try:
        seconds = int(tokens.get("expires_in") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        expires_at = (datetime.now(timezone.utc)
                      + timedelta(seconds=seconds)).isoformat()
    detail = {
        "access_token": tokens.get("access_token") or "",
        "refresh_token": tokens.get("refresh_token") or "",
        "provider_user_id": tokens.get("provider_user_id") or "",
        "scope": tokens.get("scope") or "",
        "expires_in": tokens.get("expires_in"),
        "expires_at": expires_at,
        "page_id": tokens.get("page_id") or "",
        "page_name": tokens.get("page_name") or "",
        "ig_user_id": tokens.get("ig_user_id") or "",
    }
    store.update_connection(
        site_id, kind, "connected",
        note or f"Connected — {kind} account linked.", detail=detail)


def _social_client_for(store: SaasStore, config: SaasConfig,
                       site_id: str, provider: str, client=None):
    """A live provider client for a connected site, refreshing a
    stale token first. Facebook/Instagram only — X stays on main's
    inline x_post path. `client` is the injected httpx-like client
    for tests; None builds a real one. Module-level (not a closure)
    so tests can monkeypatch this name — the routes never build a
    real client in a test run."""
    detail = (store.get_connection(site_id, provider) or {}
              ).get("_detail_raw") or {}
    token = refresh_access_token_if_needed(
        store, config, site_id, provider, client=client)
    if provider == "facebook":
        page_id = detail.get("page_id") or ""
        if not page_id:
            raise SaasError(
                "No Facebook Page is saved for this site — "
                "connect Facebook and pick a Page first.")
        return FacebookClient(page_id, token, client=client)
    if provider == "instagram":
        ig_user_id = detail.get("ig_user_id") or ""
        if not ig_user_id:
            raise SaasError(
                "No Instagram Business account is saved for this "
                "site — connect Instagram again.")
        return InstagramClient(ig_user_id, token, client=client)
    raise SaasError(f"Unknown social provider: {provider}")


def _post_to_meta(store: SaasStore, config: SaasConfig, account: dict,
                  site_id: str, draft_id: str, provider: str,
                  client=None) -> tuple[bool, str]:
    """Approve-and-post one pending Facebook/Instagram draft. The
    ONLY path that ever calls the real provider post API for that
    provider — creating a draft never posts it, and a draft that is
    not explicitly approved here is never published. Mirrors
    post_to_x: the publishing flag, the posting gate, and the spend
    caps are enforced before anything is sent. Returns
    (posted, message)."""
    if not config.social_publishing_enabled:
        return False, ("Social publishing is off — the operator has not "
                       "enabled it yet, so this draft cannot publish.")
    allowed, reason = check_posting_allowed(store, account, site_id,
                                            provider, config)
    if not allowed:
        return False, reason
    draft = store.get_social_draft(draft_id)
    if draft is None or draft["site_id"] != site_id \
            or draft["kind"] != provider:
        return False, "Draft not found."
    if draft["status"] != "pending":
        return False, f"This draft was already {draft['status']}."
    has_link = bool(draft["link_url"])
    amount = estimate_cost(provider, has_link)
    ok, cap_reason = caps_allow(store, account["id"], site_id, amount,
                                config)
    if not ok:
        return False, cap_reason
    text = draft["text"]
    tag = ""
    link = ""
    if draft["link_url"]:
        site = store.get_site(site_id)
        signed, tag = sign_agent_link(
            draft["link_url"], site_id, site["host"], provider, draft_id,
            store)
        # Instagram appends the link to the caption itself (plain
        # text, not clickable) — pass it as `link`, never pre-append
        # it to the text, or the caption would carry it twice.
        link = signed
    try:
        provider_client = _social_client_for(
            store, config, site_id, provider, client=client)
        result = provider_client.publish_post(text, link)
    except ProviderAmbiguousError as exc:
        # No provider response — the post may or may not have landed.
        # The draft stays pending: never auto-retried, never decided
        # posted. The operator checks the provider before retrying.
        return False, (f"{exc} The draft is still pending — check "
                       f"{provider.capitalize()} before retrying.")
    except (ProviderError, SaasError) as exc:
        return False, str(exc)
    post_id = result.get("provider_post_id") or ""
    store.decide_social_draft(draft_id, "posted", signature_tag=tag,
                              tweet_id=post_id)
    store.record_social_spend(account["id"], site_id, provider, draft_id,
                              amount)
    label = "Facebook" if provider == "facebook" else "Instagram"
    return True, f"Posted to {label} (id {post_id})."


def post_to_facebook(store: SaasStore, config: SaasConfig, account: dict,
                     site_id: str, draft_id: str, client=None
                     ) -> tuple[bool, str]:
    """Approve-and-post one pending Facebook draft. See
    _post_to_meta."""
    return _post_to_meta(store, config, account, site_id, draft_id,
                         "facebook", client=client)


def post_to_instagram(store: SaasStore, config: SaasConfig, account: dict,
                      site_id: str, draft_id: str, client=None
                      ) -> tuple[bool, str]:
    """Approve-and-post one pending Instagram draft. See
    _post_to_meta. Caption URLs are plain text, not clickable."""
    return _post_to_meta(store, config, account, site_id, draft_id,
                         "instagram", client=client)


def register_saas_routes(app, store: SaasStore, config: SaasConfig) -> None:
    """Add the /account/... routes to a dashboard app."""

    def _account(request: Request) -> dict | None:
        return store.get_account_by_session(
            request.cookies.get(SESSION_COOKIE, ""))

    def _need_account(request: Request):
        account = _account(request)
        if account is None:
            # Sign-in lives on the landing page — that is where an
            # unsigned visitor is sent, not to a separate sign-in page.
            return None, RedirectResponse("/", status_code=303)
        return account, None

    def _signed_page(title: str, inner: str, account: dict,
                     current: str | None = None) -> str:
        """_page for a signed-in account: the platform shell (purple
        sidebar + app header). These routes are all behind the
        forced-2FA middleware gate, so the sidebar is safe here.
        Growth (the verified-customer count) comes from the store over the
        account's counted sites; pending approvals are the
        per-account pending ones from app.state.ecosystem (set at
        the top of create_app, long before register_saas_routes
        runs, so it is reachable here) — approvals for this
        account's sites, plus unassigned ones for the operator."""
        sites = store.list_sites(account["id"])
        counted_ids = [s["id"] for s in sites if s.get("count_show", 1)]
        verified = store.conversion_status_counts(counted_ids)["verified"]
        site_biz_ids = {f"site:{s['id']}" for s in sites}
        is_op = config.is_operator(account["email"])
        gate = app.state.ecosystem["approvals"]
        pending = len([a for a in gate.pending()
                       if a.business_id in site_biz_ids
                       or (is_op and a.business_id is None)])
        from ventoryn.alerts import account_alerts

        return _page(title, inner, account, current=current,
                     is_operator=is_op, sidebar=True, verified=verified,
                     pending_approvals=pending,
                     alerts=account_alerts(store, account, pending))

    def _safe_next(value: str) -> str:
        """Where a 2FA page sends the user after: a same-site path
        only, default /ops. An absolute URL is never honoured."""
        if value and value.startswith("/") and not value.startswith("//"):
            return value
        return "/ops"

    def _totp_field(account: dict) -> str:
        """The fresh-code field for a sensitive action — shown only
        when the account has 2FA enabled."""
        if not account.get("totp_enabled"):
            return ""
        return ("<label for='totp_code'>Two-factor code (fresh — "
                "required to confirm this action)</label>"
                "<input type='text' id='totp_code' name='totp_code' "
                "inputmode='numeric' autocomplete='one-time-code' "
                "maxlength='8' placeholder='6-digit code or a backup code'>")

    def _fresh_totp_ok(account: dict, totp_code: str) -> tuple[bool, str]:
        """A sensitive action re-verifies with a fresh 2FA code when
        the account has 2FA enabled (a backup code works too, and is
        burned). Without 2FA the action proceeds on the verified
        session. Returns (ok, error message)."""
        if not account.get("totp_enabled"):
            return True, ""
        code = (totp_code or "").strip()
        if code and (store.verify_totp_code(account["id"], code)
                     or store.consume_backup_code(account["id"], code)):
            return True, ""
        return False, ("Enter your current two-factor code (or a backup "
                       "code) to confirm this action.")

    def _totp_setup_page(account: dict, nxt: str, error: str = "") -> str:
        """The forced-setup page: QR + manual key + confirm form.
        Reuses a pending secret when a setup is already in progress,
        so navigating away mid-setup does not lose it."""
        secret = store.get_totp_pending_secret(account["id"])
        if not secret:
            secret = store.begin_totp_setup(account["id"])["secret"]
        uri = provisioning_uri(secret, account["email"], issuer="Ventoryn")
        err = f"<p><strong>{esc(error)}</strong></p>" if error else ""
        inner = (f"<div class='card'><h2>Two-factor authentication — "
                 f"required</h2>{err}"
                 "<p>Ventoryn requires two-factor authentication on every "
                 "account. It keeps your sites, your connections and your "
                 "revenue safe even if someone learns your Google password.</p>"
                 "<p>Scan this QR code with your authenticator app (Google "
                 "Authenticator, Authy, 1Password, or any TOTP app), then "
                 "enter the 6-digit code it shows.</p>"
                 f"<div class='qr'>{qr_svg(uri)}</div>"
                 f"<p class='muted'>Or enter this key manually: "
                 f"<code>{esc(secret)}</code></p>"
                 "<form method='post' action='/account/two-factor/setup'>"
                 f"<input type='hidden' name='next' value='{esc(nxt)}'>"
                 "<label for='code'>6-digit code from your authenticator "
                 "app</label>"
                 "<input type='text' id='code' name='code' "
                 "inputmode='numeric' autocomplete='one-time-code' required "
                 "maxlength='6'>"
                 "<p><button class='btn btn-primary' type='submit'>"
                 "Confirm and turn on two-factor</button></p></form>"
                 "<p class='muted'>Lost your phone? Your backup codes "
                 "(shown once after setup) sign you in. If you lose those "
                 "too, the operator can reset two-factor for your account "
                 "— ask him, and set it up again at your next sign-in.</p>"
                 "</div>")
        return _signed_page("Set up two-factor", inner, account,
                            current="account")

    def _totp_verify_page(account: dict, nxt: str, error: str = "") -> str:
        """The code-at-sign-in page: a TOTP code or a backup code."""
        err = f"<p><strong>{esc(error)}</strong></p>" if error else ""
        inner = (f"<div class='card'><h2>Two-factor code</h2>{err}"
                 "<p>Enter the 6-digit code from your authenticator app, "
                 "or one of your backup codes (each works once).</p>"
                 "<form method='post' action='/account/two-factor/verify'>"
                 f"<input type='hidden' name='next' value='{esc(nxt)}'>"
                 "<label for='code'>Two-factor code</label>"
                 "<input type='text' id='code' name='code' "
                 "inputmode='numeric' autocomplete='one-time-code' required "
                 "maxlength='8'>"
                 "<p><button class='btn btn-primary' type='submit'>"
                 "Verify and continue</button></p></form>"
                 "<p class='muted'>Lost your phone and your backup codes? "
                 "The operator can reset two-factor for your account — ask "
                 "him.</p></div>")
        return _signed_page("Two-factor code", inner, account,
                            current="account")

    # -- sign-in -------------------------------------------------------------
    @app.get("/account", response_class=HTMLResponse)
    def account_root(request: Request):
        account = _account(request)
        if account:
            # Everyone lands on Live Ops after sign-in — the operator
            # and every customer alike. /ops is scoped to the account.
            return RedirectResponse("/ops", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.get("/account/login", response_class=HTMLResponse)
    def login_page(request: Request):
        account = _account(request)
        if account:
            return RedirectResponse("/ops", status_code=303)
        # Sign-in belongs on the landing page (visible "Sign in with
        # Google" there), not on a bare separate sign-in page.
        return RedirectResponse("/", status_code=303)

    @app.get("/account/auth/google/start")
    def google_start(request: Request, site_url: str = ""):
        if not config.google_configured:
            return HTMLResponse(
                _page("Sign in",
                      "<p>Sign-in with Google is not set up yet.</p>"),
                status_code=400)
        state = store.create_oauth_state()
        response = RedirectResponse(google_auth_url(config, state),
                                    status_code=303)
        # Carry the website the visitor typed on the landing page
        # across the Google round-trip. An invalid URL is ignored —
        # the visitor simply signs in without one.
        pending = ""
        if site_url:
            try:
                pending = validate_site_url(site_url)
            except ValueError:
                pending = ""
        if pending:
            response.set_cookie(
                PENDING_SITE_COOKIE, pending, httponly=True,
                samesite="lax", path="/",
                max_age=PENDING_SITE_TTL_SECONDS,
                secure=config.cookie_secure)
        return response

    @app.get("/account/auth/google/callback", response_class=HTMLResponse)
    def google_callback(request: Request, code: str = "",
                        state: str = "", error: str = ""):
        if error or not code:
            return HTMLResponse(
                _page("Sign in",
                      "<p>Google sign-in did not complete. "
                      "<a href='/account/login'>Try again</a>.</p>"),
                status_code=400)
        if not store.consume_oauth_state(state):
            return HTMLResponse(
                _page("Sign in",
                      "<p>This sign-in link is not valid or has expired. "
                      "<a href='/account/login'>Start again</a>.</p>"),
                status_code=400)
        try:
            tokens = exchange_google_code(config, code)
            profile = google_userinfo(tokens["access_token"])
        except SaasError as exc:
            return HTMLResponse(
                _page("Sign in",
                      f"<p>{esc(exc)}</p><p><a href='/account/login'>"
                      "Try again</a>.</p>"),
                status_code=400)
        account = store.get_account_by_sub(profile["sub"])
        if account is None:
            account = store.create_account(profile["sub"], profile["email"],
                                           profile.get("name", ""))
        token = store.create_session(account["id"])
        # The admin's recent-activity feed: a real sign-in just
        # happened.
        store.record_activity_event(account["id"], "sign-in",
                                    "Signed in with Google")
        # The ShipItAyiti flow: after sign-in EVERYONE goes straight
        # to Live Ops (/ops) — the operator and every customer alike.
        # /ops is scoped to the signed-in account, so a customer sees
        # only their own sites and work. A website typed on the
        # landing page rides along as ?site_url=…, so /ops can offer
        # the subscribe / add-site step for it; the pending cookie is
        # consumed here either way.
        pending = request.cookies.get(PENDING_SITE_COOKIE, "")
        try:
            pending = validate_site_url(pending) if pending else ""
        except ValueError:
            pending = ""
        target = "/ops"
        if pending:
            target += ("?site_url=" +
                       urllib.parse.quote(pending, safe=""))
        # Two-factor is mandatory from the first sign-in: a fresh
        # session has not passed it yet. No 2FA set up -> the setup
        # page; 2FA enabled -> the code page. `next` carries the
        # Live Ops destination (with the pending site URL).
        nxt = urllib.parse.quote(target, safe="")
        if not store.totp_state(account["id"])["enabled"]:
            dest = f"/account/two-factor/setup?next={nxt}"
        else:
            dest = f"/account/two-factor/verify?next={nxt}"
        response = RedirectResponse(dest, status_code=303)
        response.delete_cookie(PENDING_SITE_COOKIE, path="/")
        response.set_cookie(SESSION_COOKIE, token, httponly=True,
                            samesite="lax", path="/",
                            max_age=SESSION_TTL_DAYS * 24 * 3600,
                            secure=config.cookie_secure)
        return response

    # -- X (Twitter) connection: OAuth 2.0 with PKCE, per site ---------------
    # Connecting an account never posts anything — see post_to_x / the
    # social draft routes below for the only path that does.
    @app.get("/websites/sites/{site_id}/connections/x/start")
    def x_connect_start(request: Request, site_id: str, return_to: str = ""):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        if not config.x_oauth_configured or not config.app_registered("x"):
            return HTMLResponse(
                _signed_page(site["url"],
                      "<p>X is not available yet — Ventoryn's own app "
                      "registration for it is still needed. No account "
                      "was connected and nothing was stored.</p>"
                      f"<p><a href='/websites/sites/{esc(site_id)}'>"
                      "Back</a>.</p>", account, current="websites"),
                status_code=400)
        verifier, challenge = pkce_pair()
        state = store.create_connection_oauth_state(site_id, "x", verifier)
        response = RedirectResponse(x_auth_url(config, state, challenge),
                                    status_code=303)
        # Remember where to come back to across the X round trip (an
        # Integrations window); only a same-site /integrations/ path.
        back = _integrations_return(return_to)
        if back:
            # URL-encoded: ?, = and quotes never reach the cookie value.
            response.set_cookie(X_RETURN_COOKIE, urllib.parse.quote(back, safe="/"),
                                max_age=900, httponly=True,
                                samesite='lax', path='/', secure=config.cookie_secure)
        return response

    @app.get("/account/auth/x/callback", response_class=HTMLResponse)
    def x_connect_callback(request: Request, code: str = "",
                           state: str = "", error: str = ""):
        account = _account(request)
        pending = store.consume_connection_oauth_state(state)
        if error or not code or pending is None or pending["kind"] != "x":
            return HTMLResponse(
                _page("Connect X",
                      "<p>Connecting X did not complete. "
                      "<a href='/account/login'>Back</a>.</p>"),
                status_code=400)
        site = store.get_site(pending["site_id"])
        if (site is None or account is None
                or site["account_id"] != account["id"]):
            return HTMLResponse(
                _page("Connect X",
                      "<p>This connection link is not valid.</p>"),
                status_code=400)
        try:
            tokens = exchange_x_code(config, code, pending["code_verifier"])
        except SaasError as exc:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(str(exc))}</p>"
                      f"<p><a href='/websites/sites/{esc(site['id'])}'>"
                      "Back</a>.</p>", account, current="websites"),
                status_code=400)
        access_token = tokens.get("access_token", "")
        info = x_userinfo(access_token) if access_token else {}
        username = (info.get("data") or {}).get("username", "")
        store.update_connection(
            site["id"], "x", "connected",
            f"Connected as @{username}." if username else "Connected.",
            {"access_token": access_token,
             "refresh_token": tokens.get("refresh_token", ""),
             "username": username})
        back = _integrations_return(urllib.parse.unquote(
            request.cookies.get(X_RETURN_COOKIE, "").strip('"')))
        response = RedirectResponse(back or f"/websites/sites/{site['id']}",
                                    status_code=303)
        response.delete_cookie(X_RETURN_COOKIE, path='/')
        return response

    def _finish_facebook_page(site_id: str, user_token: str,
                              page_id: str):
        """Exchange a user token for one Page's token and save it.
        Returns the site redirect; raises SaasError when the Page
        cannot be reached."""
        page = exchange_for_page_token(user_token, config,
                                       page_id=page_id or None)
        tokens = {
            "access_token": page.get("access_token") or "",
            "provider_user_id": page.get("page_id") or "",
            "page_id": page.get("page_id") or "",
            "page_name": page.get("page_name") or "",
            "ig_user_id": (page.get("instagram_business_account_id")
                           or ""),
        }
        _save_social_tokens(
            store, site_id, "facebook", tokens,
            f"Connected — Facebook Page {page.get('page_name') or ''} "
            "linked.")
        return RedirectResponse(f"/websites/sites/{site_id}",
                                status_code=303)

    @app.get("/websites/sites/{site_id}/connections/facebook/start")
    def facebook_connect_start(request: Request, site_id: str):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        if not config.facebook_configured \
                or not config.app_registered("facebook"):
            return HTMLResponse(
                _signed_page(site["url"],
                      "<p>Facebook is not available yet — Ventoryn's own "
                      "Meta app registration for it is still needed. No "
                      "account was connected and nothing was stored.</p>"
                      f"<p><a href='/websites/sites/{esc(site_id)}'>"
                      "Back</a>.</p>", account, current="websites"),
                status_code=400)
        state = store.create_connection_oauth_state(site_id, "facebook",
                                                    "")
        url, _ = build_authorize_url(
            "facebook", config,
            f"{config.base_url}/account/auth/facebook/callback", state)
        return RedirectResponse(url, status_code=303)

    @app.get("/account/auth/facebook/callback", response_class=HTMLResponse)
    def facebook_connect_callback(request: Request, code: str = "",
                                  state: str = "", error: str = ""):
        account = _account(request)
        pending = store.consume_connection_oauth_state(state)
        if error or not code or pending is None \
                or pending["kind"] != "facebook":
            return HTMLResponse(
                _page("Connect Facebook",
                      "<p>Connecting Facebook did not complete. "
                      "<a href='/account/login'>Back</a>.</p>"),
                status_code=400)
        site = store.get_site(pending["site_id"])
        site_id = pending["site_id"]
        if (site is None or account is None
                or site["account_id"] != account["id"]):
            return HTMLResponse(
                _page("Connect Facebook",
                      "<p>This connection link is not valid.</p>"),
                status_code=400)
        redirect_uri = f"{config.base_url}/account/auth/facebook/callback"
        try:
            tokens = exchange_code("facebook", config, code, redirect_uri)
            long_lived = exchange_long_lived_token(
                tokens.get("access_token") or "", config)
        except SaasError as exc:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(str(exc))}</p>"
                      f"<p><a href='/websites/sites/{esc(site['id'])}'>"
                      "Back</a>.</p>", account, current="websites"),
                status_code=400)
        user_token = long_lived.get("access_token") \
            or tokens.get("access_token") or ""
        # Facebook: stash the long-lived user token (still
        # not_connected) so the Page picker can exchange it for a
        # Page token. The token is encrypted at rest like any
        # other stored token.
        store.update_connection(
            site_id, "facebook", "not_connected",
            "Pick a Page to finish connecting.",
            {"access_token": user_token,
             "provider_user_id": tokens.get("provider_user_id") or "",
             "scope": tokens.get("scope") or ""})
        try:
            pages = list_facebook_pages(user_token, config)
        except SaasError as exc:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(str(exc))}</p>"
                      f"<p><a href='/websites/sites/{esc(site['id'])}'>"
                      "Back</a>.</p>", account, current="websites"),
                status_code=400)
        if not pages:
            return HTMLResponse(
                _signed_page(
                    site["url"],
                    "<p>No Facebook Page was found for this account — "
                    "create or manage a Page first, then connect "
                    "again.</p><p><a href='/websites/sites/"
                    f"{esc(site_id)}'>Back</a>.</p>",
                    account, current="websites"),
                status_code=400)
        if len(pages) == 1:
            try:
                return _finish_facebook_page(site_id, user_token,
                                             pages[0]["id"])
            except SaasError as exc:
                return HTMLResponse(
                    _signed_page(site["url"],
                          f"<p>{esc(str(exc))}</p>"
                          f"<p><a href='/websites/sites/{esc(site['id'])}'>"
                          "Back</a>.</p>", account, current="websites"),
                    status_code=400)
        options = "".join(
            f"<option value='{esc(p['id'])}'>{esc(p['name'])}</option>"
            for p in pages)
        inner = (f"<div class='card'><h2>Pick your Facebook Page</h2>"
                 "<p class='muted'>Ventoryn will publish to the Page "
                 "you pick, only after you approve each post.</p>"
                 "<form method='post' action='/account/auth/facebook/page'>"
                 f"<input type='hidden' name='site_id' value='{esc(site_id)}'>"
                 "<label for='fb_page'>Page</label>"
                 f"<select id='fb_page' name='page_id'>{options}</select>"
                 "<p><button class='btn btn-primary' type='submit'>"
                 "Connect this Page</button></p></form></div>")
        return HTMLResponse(_signed_page(site["url"], inner, account,
                                         current="websites"))

    @app.post("/account/auth/facebook/page")
    def facebook_page_pick(request: Request, site_id: str = Form(""),
                           page_id: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id) if site_id else None
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Connect", "<p>Website not found.</p>",
                             account, current="account"), status_code=404)
        conn = store.get_connection(site_id, "facebook") or {}
        user_token = (conn.get("_detail_raw") or {}
                      ).get("access_token") or ""
        if not user_token:
            return HTMLResponse(
                _signed_page(site["url"],
                      "<p>The Facebook connection expired — start the "
                      "connection again.</p><p><a href='/websites/sites/"
                      f"{esc(site_id)}'>Back</a>.</p>",
                      account, current="account"),
                status_code=400)
        try:
            return _finish_facebook_page(site_id, user_token,
                                         page_id.strip())
        except SaasError as exc:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(str(exc))}</p>"
                      f"<p><a href='/websites/sites/{esc(site['id'])}'>"
                      "Back</a>.</p>", account, current="account"),
                status_code=400)

    @app.get("/websites/sites/{site_id}/connections/instagram/start")
    def instagram_connect_start(request: Request, site_id: str):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        if not config.facebook_configured \
                or not config.app_registered("instagram"):
            return HTMLResponse(
                _signed_page(site["url"],
                      "<p>Instagram is not available yet — Ventoryn's own "
                      "Meta app registration for it is still needed. No "
                      "account was connected and nothing was stored.</p>"
                      f"<p><a href='/websites/sites/{esc(site_id)}'>"
                      "Back</a>.</p>", account, current="websites"),
                status_code=400)
        state = store.create_connection_oauth_state(site_id, "instagram",
                                                    "")
        url, _ = build_authorize_url(
            "instagram", config,
            f"{config.base_url}/account/auth/instagram/callback", state)
        return RedirectResponse(url, status_code=303)

    @app.get("/account/auth/instagram/callback", response_class=HTMLResponse)
    def instagram_connect_callback(request: Request, code: str = "",
                                   state: str = "", error: str = ""):
        account = _account(request)
        pending = store.consume_connection_oauth_state(state)
        if error or not code or pending is None \
                or pending["kind"] != "instagram":
            return HTMLResponse(
                _page("Connect Instagram",
                      "<p>Connecting Instagram did not complete. "
                      "<a href='/account/login'>Back</a>.</p>"),
                status_code=400)
        site = store.get_site(pending["site_id"])
        site_id = pending["site_id"]
        if (site is None or account is None
                or site["account_id"] != account["id"]):
            return HTMLResponse(
                _page("Connect Instagram",
                      "<p>This connection link is not valid.</p>"),
                status_code=400)
        redirect_uri = f"{config.base_url}/account/auth/instagram/callback"
        try:
            tokens = exchange_code("instagram", config, code, redirect_uri)
            long_lived = exchange_long_lived_token(
                tokens.get("access_token") or "", config)
        except SaasError as exc:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(str(exc))}</p>"
                      f"<p><a href='/websites/sites/{esc(site['id'])}'>"
                      "Back</a>.</p>", account, current="websites"),
                status_code=400)
        user_token = long_lived.get("access_token") \
            or tokens.get("access_token") or ""
        try:
            ig_user_id = find_instagram_account(user_token, config)
        except SaasError as exc:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(str(exc))}</p>"
                      f"<p><a href='/websites/sites/{esc(site['id'])}'>"
                      "Back</a>.</p>", account, current="websites"),
                status_code=400)
        if not ig_user_id:
            return HTMLResponse(
                _signed_page(
                    site["url"],
                    "<p>No Instagram Business account is linked to "
                    "a Facebook Page you manage — link one in the "
                    "Meta Business Suite first, then connect "
                    "again.</p><p><a href='/websites/sites/"
                    f"{esc(site_id)}'>Back</a>.</p>",
                    account, current="websites"),
                status_code=400)
        ig_tokens = dict(tokens)
        ig_tokens["access_token"] = user_token
        ig_tokens["ig_user_id"] = ig_user_id
        if long_lived.get("expires_in"):
            ig_tokens["expires_in"] = long_lived["expires_in"]
        _save_social_tokens(
            store, site_id, "instagram", ig_tokens,
            "Connected — Instagram Business account linked.")
        return RedirectResponse(f"/websites/sites/{site_id}",
                                status_code=303)

    def _x_estimate_card(site_id: str, spent: float) -> str:
        """What an X campaign would cost, shown before anything is
        posted: 1 linked post a day for 30 days by default, recalculated
        in the page as the owner changes the plan (same rates)."""
        est = estimate_x_campaign(posts_per_day=1, days=30, with_link=True,
                                  spent_this_month_usd=spent,
                                  monthly_budget_usd=config.x_monthly_budget_usd)
        return (
            f"<div class='card v-estimate' id='x-estimate' "
            f"data-plain='{X_RATE_PLAIN_POST_USD}' data-link='{X_RATE_LINKED_POST_USD}' "
            f"data-left='{est['budget_left_usd']}' data-budget='{est['monthly_budget_usd']}'>"
            f"<h4>Campaign cost estimate</h4>"
            f"<div class='v-estimate-inputs'>"
            f"<label>Posts a day <input type='number' id='est-ppd' min='0' "
            f"max='{MAX_POSTS_PER_DAY}' step='1' value='1'></label>"
            f"<label>Days <input type='number' id='est-days' min='1' "
            f"max='{MAX_DAYS}' step='1' value='30'></label>"
            f"<label><input type='checkbox' id='est-link' checked> "
            f"With a link (signed, so sales are counted)</label></div>"
            f"<p id='est-out'>{esc(describe_estimate(est))}</p>"
            f"<p class='muted'>{esc(X_RATE_SOURCE)}. X bills each post to the "
            f"developer account behind Ventoryn's X app; Ventoryn stops posting "
            f"at this website's monthly X budget.</p>"
            "<style>.v-estimate-inputs{display:flex;flex-wrap:wrap;gap:12px 20px;"
            "align-items:flex-end}.v-estimate-inputs label{margin:0}"
            ".v-estimate-inputs input[type=number]{width:110px;display:block;margin-top:4px}"
            "#est-out{font-weight:600}</style>"
            "<script>(function(){var c=document.getElementById('x-estimate');if(!c)return;"
            "var p=document.getElementById('est-ppd'),d=document.getElementById('est-days'),"
            "l=document.getElementById('est-link'),o=document.getElementById('est-out');"
            "function f(n){return '$'+n.toFixed(2);}"
            "function run(){var ppd=Math.max(0,Math.min(" + str(MAX_POSTS_PER_DAY) + ",+p.value||0)),"
            "days=Math.max(1,Math.min(" + str(MAX_DAYS) + ",Math.floor(+d.value||1))),"
            "rate=+(l.checked?c.dataset.link:c.dataset.plain),posts=Math.round(ppd*days),"
            "total=Math.round(posts*rate*100)/100,left=+c.dataset.left,budget=+c.dataset.budget,"
            "kind=l.checked?'with a link':'without a link',"
            "s=posts+' X posts '+kind+' over '+days+' days at $'+rate.toFixed(3)+' each: about '+f(total)+'.';"
            "if(total<=left){s+=\" Fits this month's X budget (\"+f(left)+' of '+f(budget)+' left).';}"
            "else{s+=' That is more than the '+f(left)+\" left in this month's \"+f(budget)+"
            "' X budget \u2014 it covers '+Math.min(posts,Math.floor(Math.round(left*1000)/Math.round(rate*1000)))+' of those posts.';}"
            "o.textContent=s;}"
            "[p,d,l].forEach(function(el){el.addEventListener('input',run);el.addEventListener('change',run);});"
            "})();</script></div>")

    # -- X social drafts: compose -> approve-and-post, never automatic ------
    @app.post("/websites/sites/{site_id}/social/x/drafts")
    def x_draft_create(request: Request, site_id: str,
                       text: str = Form(...), link_url: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        if text.strip():
            store.create_social_draft(site_id, "x", text.strip(),
                                      link_url.strip())
        return RedirectResponse(f"/websites/sites/{site_id}", status_code=303)

    @app.post("/websites/sites/{site_id}/social/x/drafts/{draft_id}/approve")
    def x_draft_approve(request: Request, site_id: str, draft_id: str,
                        totp_code: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        ok, err = _fresh_totp_ok(account, totp_code)
        if not ok:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(err)}</p><p><a href='/websites/sites/"
                      f"{esc(site_id)}'>Back</a>.</p>",
                      account, current="websites"), status_code=400)
        posted, message = post_to_x(store, config, account, site_id, draft_id)
        inner = (f"<p>{esc(message)}</p>"
                 f"<p><a href='/websites/sites/{esc(site_id)}'>Back to "
                 f"{esc(site['url'])}</a>.</p>")
        return HTMLResponse(
            _signed_page(site["url"], inner, account, current="websites"),
            status_code=200 if posted else 400)

    @app.post("/websites/sites/{site_id}/social/x/drafts/{draft_id}/reject")
    def x_draft_reject(request: Request, site_id: str, draft_id: str):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        store.decide_social_draft(draft_id, "rejected")
        return RedirectResponse(f"/websites/sites/{site_id}", status_code=303)

    # -- Facebook social drafts: compose -> approve-and-post, never automatic
    @app.post("/websites/sites/{site_id}/social/facebook/drafts")
    def facebook_draft_create(request: Request, site_id: str,
                              text: str = Form(...),
                              link_url: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        if text.strip():
            store.create_social_draft(site_id, "facebook", text.strip(),
                                      link_url.strip())
        return RedirectResponse(f"/websites/sites/{site_id}", status_code=303)

    @app.post("/websites/sites/{site_id}/social/facebook/drafts/{draft_id}/approve")
    def facebook_draft_approve(request: Request, site_id: str, draft_id: str,
                               totp_code: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        ok, err = _fresh_totp_ok(account, totp_code)
        if not ok:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(err)}</p><p><a href='/websites/sites/"
                      f"{esc(site_id)}'>Back</a>.</p>",
                      account, current="websites"),
                status_code=400)
        posted, message = post_to_facebook(store, config, account, site_id,
                                           draft_id)
        inner = (f"<p>{esc(message)}</p>"
                 f"<p><a href='/websites/sites/{esc(site_id)}'>Back to "
                 f"{esc(site['url'])}</a>.</p>")
        return HTMLResponse(
            _signed_page(site["url"], inner, account, current="websites"),
            status_code=200 if posted else 400)

    @app.post("/websites/sites/{site_id}/social/facebook/drafts/{draft_id}/reject")
    def facebook_draft_reject(request: Request, site_id: str, draft_id: str):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        store.decide_social_draft(draft_id, "rejected")
        return RedirectResponse(f"/websites/sites/{site_id}", status_code=303)

    # -- Instagram social drafts: compose -> approve-and-post, never automatic
    @app.post("/websites/sites/{site_id}/social/instagram/drafts")
    def instagram_draft_create(request: Request, site_id: str,
                               text: str = Form(...),
                               link_url: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        if text.strip():
            store.create_social_draft(site_id, "instagram", text.strip(),
                                      link_url.strip())
        return RedirectResponse(f"/websites/sites/{site_id}", status_code=303)

    @app.post("/websites/sites/{site_id}/social/instagram/drafts/{draft_id}/approve")
    def instagram_draft_approve(request: Request, site_id: str, draft_id: str,
                                totp_code: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        ok, err = _fresh_totp_ok(account, totp_code)
        if not ok:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(err)}</p><p><a href='/websites/sites/"
                      f"{esc(site_id)}'>Back</a>.</p>",
                      account, current="websites"),
                status_code=400)
        posted, message = post_to_instagram(store, config, account, site_id,
                                            draft_id)
        inner = (f"<p>{esc(message)}</p>"
                 f"<p><a href='/websites/sites/{esc(site_id)}'>Back to "
                 f"{esc(site['url'])}</a>.</p>")
        return HTMLResponse(
            _signed_page(site["url"], inner, account, current="websites"),
            status_code=200 if posted else 400)

    @app.post("/websites/sites/{site_id}/social/instagram/drafts/{draft_id}/reject")
    def instagram_draft_reject(request: Request, site_id: str, draft_id: str):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(
                _signed_page("Website", "<p>Website not found.</p>",
                            account, current="websites"), status_code=404)
        store.decide_social_draft(draft_id, "rejected")
        return RedirectResponse(f"/websites/sites/{site_id}", status_code=303)

    @app.get("/account/logout")
    def logout(request: Request):
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            store.delete_session(token)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # -- two-factor (forced) + security ----------------------------------
    @app.get("/account/two-factor/setup", response_class=HTMLResponse)
    def totp_setup_page(request: Request, next: str = "/ops"):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        if store.totp_state(account["id"])["enabled"]:
            return RedirectResponse("/account/security", status_code=303)
        return HTMLResponse(
            _totp_setup_page(account, _safe_next(next)))

    @app.post("/account/two-factor/setup", response_class=HTMLResponse)
    def totp_setup_confirm(request: Request, code: str = Form(""),
                           next: str = Form("/ops")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        if store.totp_state(account["id"])["enabled"]:
            return RedirectResponse(_safe_next(next), status_code=303)
        if store.confirm_totp_setup(account["id"], code.strip()):
            token = request.cookies.get(SESSION_COOKIE, "")
            if token:
                store.mark_session_totp_verified(token)
            codes = store.regenerate_backup_codes(account["id"])
            codes_html = "".join(
                f"<div>{esc(c[:4])} {esc(c[4:])}</div>" for c in codes)
            dest = _safe_next(next)
            inner = (f"<div class='card'><h2>Two-factor is on</h2>"
                     "<p>Your account is protected. Save these backup "
                     "codes now — each works once, and they are the way "
                     "back in if you lose your phone. They are shown "
                     "only this once.</p>"
                     f"<div class='codes'>{codes_html}</div>"
                     f"<p><a class='btn btn-primary' href='{esc(dest)}'>"
                     "Continue to Overview</a></p></div>")
            return HTMLResponse(_signed_page("Two-factor is on", inner,
                                            account, current="account"))
        return HTMLResponse(
            _totp_setup_page(account, _safe_next(next),
                             "That code did not match — check your "
                             "authenticator app and try again."))

    @app.get("/account/two-factor/verify", response_class=HTMLResponse)
    def totp_verify_page(request: Request, next: str = "/ops"):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        if not store.totp_state(account["id"])["enabled"]:
            return RedirectResponse(
                "/account/two-factor/setup?next=" +
                urllib.parse.quote(_safe_next(next), safe=""),
                status_code=303)
        session = store.get_session(request.cookies.get(SESSION_COOKIE, ""))
        if session and session.get("totp_verified"):
            return RedirectResponse(_safe_next(next), status_code=303)
        return HTMLResponse(
            _totp_verify_page(account, _safe_next(next)))

    @app.post("/account/two-factor/verify", response_class=HTMLResponse)
    def totp_verify(request: Request, code: str = Form(""),
                    next: str = Form("/ops")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        c = code.strip()
        if c and (store.verify_totp_code(account["id"], c)
                  or store.consume_backup_code(account["id"], c)):
            token = request.cookies.get(SESSION_COOKIE, "")
            if token:
                store.mark_session_totp_verified(token)
            return RedirectResponse(_safe_next(next), status_code=303)
        return HTMLResponse(
            _totp_verify_page(account, _safe_next(next),
                              "That code did not work — try the current "
                              "code from your authenticator app, or a "
                              "backup code."))

    @app.get("/account/security", response_class=HTMLResponse)
    def security_page(request: Request):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        state = store.totp_state(account["id"])
        if state["enabled"]:
            twofa = ("<p>Two-factor authentication: "
                     "<strong>Enabled</strong> — a code is required at "
                     "every sign-in.</p>")
        else:
            twofa = ("<p>Two-factor authentication: "
                     "<strong>Not set up</strong> — "
                     "<a href='/account/two-factor/setup'>Set up "
                     "two-factor</a>. You cannot use Ventoryn until it "
                     "is set up.</p>")
        paused = bool(account.get("posting_paused"))
        pause = (f"<div class='card'><h2>Posting</h2><p>"
                 + ("Posting is PAUSED — no agent will post or reply "
                    "as you." if paused else
                    "Posting is on — agents may prepare posts and replies "
                    "(your approval first).")
                 + f"</p><form method='post' "
                 f"action='/account/security/pause'>"
                 f"<input type='hidden' name='paused' value="
                 f"'{'0' if paused else '1'}'>"
                 f"<button class='btn' type='submit'>"
                 f"{'Resume posting' if paused else 'Pause all posting'}"
                 f"</button></form></div>")
        delete = ("<div class='card'><h2>Delete your account</h2>"
                  "<p>This deletes your account, your websites, your "
                  "connections (tokens are revoked at the provider "
                  "first), your jobs, your credit ledger and your "
                  "revenue data. It cannot be undone.</p>"
                  "<form method='post' action='/account/delete'>"
                  f"{_totp_field(account)}"
                  "<p><button class='btn' type='submit'>Delete my "
                  "account</button></p></form></div>")
        inner = (f"{_account_subnav_html('security')}"
                 f"<div class='card'><h2>Two-factor authentication</h2>"
                 f"{twofa}</div>{pause}{delete}")
        return _signed_page("Security", inner, account, current="account")

    @app.post("/account/security/pause")
    def security_pause(request: Request, paused: str = Form("0")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        store.set_posting_paused(account["id"], paused == "1")
        return RedirectResponse("/account/security", status_code=303)

    @app.post("/account/delete")
    def delete_account_route(request: Request,
                             totp_code: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        ok, err = _fresh_totp_ok(account, totp_code)
        if not ok:
            return HTMLResponse(
                _signed_page("Security",
                      f"<p>{esc(err)}</p><p><a href='/account/security'>"
                      "Back</a>.</p>", account, current="account"),
                status_code=400)
        # Revoke at the provider first, best-effort, then delete
        # everything the account owns.
        for site in store.list_sites(account["id"]):
            for conn in store.get_connections(site["id"]):
                raw = conn.get("_detail_raw") or {}
                if raw:
                    revoke_provider_token(conn["kind"], raw)
        store.delete_account(account["id"])
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            store.delete_session(token)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # -- customer dashboard --------------------------------------------------
    @app.get("/account/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, subscribed: str = "",
                  site_url: str = ""):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        sites = store.list_sites(account["id"])
        # The website the visitor typed on the landing page, carried
        # through sign-in and checkout — it arrives prefilled on the
        # add-website form, never retyped.
        try:
            pending_url = validate_site_url(site_url) if site_url else ""
        except ValueError:
            pending_url = ""
        add_site_href = ("/websites/sites/new?url=" +
                         urllib.parse.quote(pending_url, safe="") if
                         pending_url else "/websites/sites/new")
        notice = ""
        if subscribed:
            notice = ("<div class='card'><p><strong>Subscription started "
                      "— welcome.</strong> Your monthly credits are in.</p>"
                      f"<p><a class='btn btn-primary' href='{esc(add_site_href)}'>"
                      "Add your website</a></p></div>")
        status = account["subscription_status"]
        status_badge = ("<span class='badge badge-ok'>Subscribed</span>"
                        if status == "active" else
                        "<span class='badge badge-off'>Not subscribed</span>")
        # Two-factor status, one line in the plan card (the setup page
        # itself lives at /account/security).
        if store.totp_state(account["id"])["enabled"]:
            totp_line = ("<p>Two-factor authentication: "
                         "<strong>Enabled</strong> — a code is required at "
                         "every sign-in. <a href='/account/security'>"
                         "Security settings</a>.</p>")
        else:
            totp_line = ("<p>Two-factor authentication: "
                         "<strong>Not set up</strong> — "
                         "<a href='/account/security'>Set up "
                         "two-factor</a>. You cannot use Ventoryn until "
                         "it is set up.</p>")
        # The plan card: name + status + credit balance only. The
        # Subscribe pitch ($20/month) and the credit table ("what a
        # credit buys") live ONLY on the Subscribe page — My account
        # shows your plan only (websites + jobs are on Websites).
        # The owner has no $20/month plan: "Permanent — owner", no
        # price, no Subscribe button for himself.
        if config.is_operator(account["email"]):
            plan_card = (
                "<div class='card'><h2>Your plan</h2>"
                "<p><span class='badge badge-ok'>Permanent — owner"
                "</span></p>"
                "<p>The owner's account — no $20/month plan, no "
                "charge.</p>"
                "<p>Credit balance: <span class='big'>Unlimited</span>.</p>"
                f"{totp_line}"
                f"<p><a class='btn' href='{esc(add_site_href)}'>"
                "Add a website</a></p></div>")
            # The operator-global pause control (Settings): the only
            # way to stop the agents working, alongside the Pause
            # button on Overview. Same card, same endpoint.
            from ventoryn import ui as _ui

            pause_card = _ui.pause_card_html(
                store.get_global_pause(), "/account/dashboard")
        else:
            plan_card = (
                f"<div class='card'><h2>Your plan</h2>"
                f"<p>{status_badge} {esc(PLAN_NAME)}</p>"
                f"<p>Credit balance: <span class='big'>"
                f"{account['credits_balance']}</span> credits &middot; "
                f"{config.monthly_credits:,} credits included every month "
                f"while subscribed.</p>"
                f"{totp_line}"
                f"<p><a class='btn' href='{esc(add_site_href)}'>"
                "Add a website</a></p></div>")
            pause_card = ""
        # Your websites (with their funnels) and Recent jobs moved to the
        # Websites page (/portfolio); My account is the plan only.
        inner = f"""{_account_subnav_html('dashboard')}{notice}
{plan_card}
{pause_card}"""
        return _signed_page("My account", inner, account, current="account")

    # -- subscribe -----------------------------------------------------------
    @app.get("/account/subscribe", response_class=HTMLResponse)
    def subscribe_page(request: Request, cancelled: str = "",
                       site_url: str = ""):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        # The owner has no $20/month plan: his Subscribe page shows
        # the permanent subscription and no checkout button. Regular
        # customers see the pitch and the credit table below.
        if config.is_operator(account["email"]):
            inner = (f"{_account_subnav_html('subscribe')}"
                     "<div class='card'><p><span class='badge "
                     "badge-ok'>Permanent — owner</span></p>"
                     "<p>The owner's account — no $20/month plan, no "
                     "charge, nothing to subscribe to.</p></div>")
            return _signed_page("Subscribe", inner, account,
                                current="subscribe")
        try:
            pending_url = validate_site_url(site_url) if site_url else ""
        except ValueError:
            pending_url = ""
        pending_note = ""
        pending_field = ""
        if pending_url:
            pending_note = (f"<p>After subscribing, we'll add "
                            f"<strong>{esc(pending_url)}</strong> to your "
                            f"account — the website you typed on the "
                            f"front page.</p>")
            pending_field = (f"<input type='hidden' name='site_url' "
                             f"value='{esc(pending_url)}'>")
        notice = ""
        if cancelled:
            notice = ("<div class='card'><p>Checkout was cancelled — "
                      "nothing was charged.</p></div>")
        if account["subscription_status"] == "active":
            inner = (f"{_account_subnav_html('subscribe')}{notice}"
                     f"<div class='card'><p>You are subscribed "
                     f"to {esc(PLAN_NAME)} at {esc(PLAN_PRICE_LABEL)}."
                     "</p><p>Your monthly credits arrive with each "
                     "billing cycle.</p></div>")
            if pending_url:
                inner += (f"<div class='card'>{pending_note}<p><a class='btn "
                          f"btn-primary' href='/websites/sites/new?url="
                          f"{esc(urllib.parse.quote(pending_url, safe=''))}'>"
                          f"Add {esc(pending_url)}</a></p></div>")
            return _signed_page("Subscribe", inner, account,
                                current="subscribe")
        if not config.stripe_configured:
            inner = (f"{_account_subnav_html('subscribe')}{notice}"
                     f"<div class='card'><h2>{esc(PLAN_NAME)} — "
                     f"{esc(PLAN_PRICE_LABEL)}</h2><p>What it includes:</p>"
                     f"<ul><li>{config.monthly_credits:,} credits every "
                     f"month</li><li>Your websites on Ventoryn, working "
                     f"to find new paying customers</li>"
                     f"<li>Growth reports per site</li></ul>"
                     f"<p><strong>Payments are not set up yet.</strong> "
                     f"The price is {esc(PLAN_PRICE_LABEL)}; the Stripe "
                     f"connection is still being set up, so checkout is "
                     f"not available on this page yet.</p></div>"
                     f"<h2>What a credit buys</h2>{_credit_table()}")
            return _signed_page("Subscribe", inner, account,
                                current="subscribe")
        inner = (f"{_account_subnav_html('subscribe')}{notice}"
                 f"<div class='card'><h2>{esc(PLAN_NAME)} — "
                 f"{esc(PLAN_PRICE_LABEL)}</h2><p>What it includes:</p>"
                 f"<ul><li>{config.monthly_credits:,} credits every month"
                 f"</li><li>Your websites on Ventoryn, working to find "
                 f"new paying customers</li>"
                 f"<li>Growth reports per site</li></ul>"
                 f"{pending_note}"
                 f"<form method='post' action='/account/subscribe'>"
                 f"{pending_field}"
                 f"<button class='btn btn-primary' type='submit'>"
                 f"Subscribe — $20/month</button></form>"
                 f"<p class='muted'>Checkout is handled by Stripe. "
                 f"Ventoryn never sees your card.</p></div>"
                 f"<h2>What a credit buys</h2>{_credit_table()}")
        return _signed_page("Subscribe", inner, account,
                                current="subscribe")

    @app.post("/account/subscribe")
    def subscribe_start(request: Request, site_url: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        if account["subscription_status"] == "active":
            return RedirectResponse("/account/dashboard", status_code=303)
        if config.is_operator(account["email"]):
            # The owner has no $20/month plan to buy.
            return RedirectResponse("/account/dashboard", status_code=303)
        try:
            pending_url = validate_site_url(site_url) if site_url else ""
        except ValueError:
            pending_url = ""
        try:
            session = create_checkout_session(
                config, account,
                site_url=pending_url or None)
        except SaasError as exc:
            return HTMLResponse(
                _signed_page("Subscribe", f"<p>{esc(exc)}</p>"
                      "<p><a href='/account/subscribe'>Back</a>.</p>",
                      account, current="subscribe"),
                status_code=400)
        return RedirectResponse(session["url"], status_code=303)

    @app.post("/account/wallet/topup")
    def wallet_topup_start(request: Request, amount_cents: int = Form(...)):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        try:
            session = create_wallet_topup_session(config, account, amount_cents)
        except SaasError as exc:
            return HTMLResponse(
                _signed_page("Wallet top-up", f"<p>{esc(exc)}</p>"
                      "<p><a href='/account/dashboard'>Back</a>.</p>",
                      account, current="account"),
                status_code=400)
        return RedirectResponse(session["url"], status_code=303)

    # -- sites ---------------------------------------------------------------
    @app.get("/websites/sites/new", response_class=HTMLResponse)
    def site_new_page(request: Request):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        inner = ("<div class='card'><p>Add the website Ventoryn will "
                 "work on — it works to bring this site new paying "
                 "customers.</p>"
                 "<form method='post' action='/websites/sites'>"
                 "<label for='url'>Website URL</label>"
                 "<input type='text' id='url' name='url' required "
                 "placeholder='yourwebsite.com'>"
                 "<label for='country'>Market country</label>"
                 "<select id='country' name='country'>"
                 f"{country_options('ca')}"
                 "</select>"
                 "<label for='language'>Market language</label>"
                 "<select id='language' name='language'>"
                 "<option value='en' selected>English (en)</option>"
                 "<option value='fr'>French (fr)</option>"
                 "<option value='ht'>Haitian Creole (ht)</option>"
                 "</select>"
                 "<p><button class='btn btn-primary' type='submit'>"
                 "Add website</button></p></form></div>")
        return _signed_page("Add a website", inner, account, current="websites")

    @app.post("/websites/sites")
    def site_create(request: Request, url: str = Form(...),
                    country: str = Form("ca"), language: str = Form("en")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        try:
            site = store.create_site(account["id"], url, country, language)
        except ValueError as exc:
            return HTMLResponse(
                _signed_page("Add a website",
                      f"<p>{esc(exc)}</p><p><a href='/websites/sites/new'>"
                      "Back</a>.</p>", account, current="websites"),
                status_code=400)
        # The admin's recent-activity feed: a real website was added.
        store.record_activity_event(account["id"], "site-added",
                                    f"Website added: {site['url']}")
        # The website snapshot (preview, specs, traffic, presence) starts
        # right away in the background; the site page fills in.
        from ventoryn import site_snapshot
        site_snapshot.start(store, site, REPORTS_DIR)
        return RedirectResponse(f"/websites/sites/{site['id']}",
                                status_code=303)

    @app.get("/websites/sites/{site_id}", response_class=HTMLResponse)
    def site_detail(request: Request, site_id: str, va_check: str = ""):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_signed_page("Website", "<p>Website not found.</p>",
                                      account, current="websites"), status_code=404)
        # Backfill connection rows for kinds added after this site was
        # created (facebook / instagram) — older sites gain them here.
        store.ensure_connections(site_id)
        connections = store.get_connections(site_id)
        plausible_raw = (store.get_connection(site_id, "plausible") or {}
                         ).get("_detail_raw") or {}
        x_conn = store.get_connection(site_id, "x")
        x_raw = (x_conn or {}).get("_detail_raw") or {}
        fb_raw = (store.get_connection(site_id, "facebook") or {}
                  ).get("_detail_raw") or {}
        ig_raw = (store.get_connection(site_id, "instagram") or {}
                  ).get("_detail_raw") or {}
        x_access_token = x_raw.get("access_token", "")
        plausible_key = plausible_raw.get("api_key") if \
            (store.get_connection(site_id, "plausible") or {}
             ).get("status") == "connected" else None
        funnel = _funnel_rows(store, site, plausible_key)
        conn_rows = ""
        for conn in connections:
            kind = conn["kind"]
            badge = ("<span class='badge badge-ok'>Connected</span>" if
                     conn["status"] == "connected" else
                     "<span class='badge badge-off'>Not connected</span>")
            consent = CONNECTION_CONSENT.get(kind, "")
            scopes = CONNECTION_SCOPES.get(kind, "")
            gate_html = ""
            if kind in ("reddit", "x", "facebook", "instagram"):
                _gate_ok, gate_reason = check_posting_allowed(
                    store, account, site_id, kind, config)
                gate_html = f"<p>Posting: {esc(gate_reason)}</p>"
            disconnect_html = ""
            if conn["status"] == "connected":
                disconnect_html = (
                    f"<form method='post' action='/websites/sites/{esc(site_id)}"
                    f"/connections/{kind}/disconnect'>"
                    f"{_totp_field(account)}"
                    "<p><button class='btn' type='submit'>Disconnect</button>"
                    "</p></form>")
            if kind == "plausible":
                form = ("<form method='post' "
                        f"action='/websites/sites/{esc(site_id)}/connections/plausible'>"
                        "<label for='plausible_domain'>Plausible site domain</label>"
                        "<input type='text' id='plausible_domain' "
                        "name='site_domain' "
                        f"value='{esc(plausible_raw.get('site_domain', site['host']))}'>"
                        "<label for='plausible_key'>Plausible API key</label>"
                        "<input type='text' id='plausible_key' name='api_key' "
                        "placeholder='Stored, never shown back'>"
                        "<p><button class='btn' type='submit'>"
                        "Connect Plausible</button></p></form>")
            elif kind == "stripe_sales":
                webhook_url = (f"{config.base_url}/account/billing/webhook")
                form = (f"<p class='muted'>Point your Stripe webhook at "
                        f"<code>{esc(webhook_url)}</code> and add "
                        f"<code>ventoryn_site_id</code> = "
                        f"<code>{esc(site_id)}</code> to your checkout "
                        f"metadata, so sales are counted for this site.</p>"
                        f"<form method='post' action='/websites/sites/{esc(site_id)}"
                        f"/connections/stripe_sales'>"
                        "<label for='stripe_secret'>Stripe webhook signing "
                        "secret</label><input type='password' id='stripe_secret' "
                        "name='webhook_secret' autocomplete='off' "
                        "spellcheck='false' placeholder='whsec_… (stored "
                        "encrypted, never shown back)'><p><button class='btn' type='submit'>"
                        "Connect Stripe sales</button></p></form>")
            elif kind == "x":
                if conn["status"] == "connected":
                    summary = x_account_summary(x_access_token) \
                        if x_access_token else None
                    audience = (
                        f"<p class='muted'>Audience: {summary['followers']} "
                        f"followers, {summary['following']} following, "
                        f"{summary['posts']} posts.</p>" if summary else
                        "<p class='muted'>Audience numbers unavailable "
                        "right now.</p>")
                    drafts = [d for d in store.list_social_drafts(site_id)
                              if d["kind"] == "x"]
                    x_spent = store.x_spend_this_month_usd(site_id)
                    draft_rows = "".join(
                        f"<div class='card'><p>{esc(d['text'])}</p>"
                        + (f"<p class='muted'>Link: {esc(d['link_url'])}"
                           "</p>" if d["link_url"] else "")
                        + (f"<p class='v-cost-line'>Cost if posted: about "
                           f"<strong>${x_post_cost_usd(bool(d['link_url'])):.3f}</strong> on X "
                           f"({'post with a link' if d['link_url'] else 'plain post'}, "
                           f"reported rate). X budget this month: "
                           f"${x_spent:.2f} of ${config.x_monthly_budget_usd:.2f} used.</p>"
                           if d["status"] == "pending" else "")
                        + f"<p>Status: <strong>{esc(d['status'])}</strong>"
                        + (f" (tweet {esc(d['tweet_id'])})"
                           if d["tweet_id"] else "") + "</p>"
                        + (f"<form method='post' action='/websites/sites/"
                           f"{esc(site_id)}/social/x/drafts/{esc(d['id'])}"
                           f"/approve'>{_totp_field(account)}<p>"
                           "<button class='btn btn-primary' type='submit'>"
                           "Approve &amp; post</button> "
                           "<button class='btn' type='submit' "
                           f"formaction='/websites/sites/{esc(site_id)}"
                           f"/social/x/drafts/{esc(d['id'])}/reject'>"
                           "Reject</button></p></form>"
                           if d["status"] == "pending" else "")
                        + "</div>"
                        for d in drafts) or (
                        "<p class='muted'>No drafts yet.</p>")
                    form = (audience + _x_estimate_card(site_id, x_spent) +
                            "<form method='post' action='/websites/sites/"
                            f"{esc(site_id)}/social/x/drafts'>"
                            "<label for='x_text'>Draft a post</label>"
                            "<textarea id='x_text' name='text' maxlength="
                            "'280' required></textarea>"
                            "<label for='x_link'>Link to sign (optional — "
                            "a URL on your own site)</label>"
                            "<input type='text' id='x_link' name='link_url' "
                            "placeholder='https://yoursite.com/...'>"
                            "<p><button class='btn' type='submit'>Save "
                            "draft</button></p></form>"
                            "<p class='muted'>Nothing is posted until you "
                            "click Approve &amp; post on a draft.</p>"
                            f"<h4>Drafts</h4>{draft_rows}")
                elif config.x_oauth_configured and config.app_registered("x"):
                    form = (f"<p class='muted'>{esc(conn['note'])}</p>"
                            f"<p><a class='btn btn-primary' href='/websites/"
                            f"sites/{esc(site_id)}/connections/x/start'>"
                            "Connect with X</a></p>")
                else:
                    form = (f"<p class='muted'>{esc(conn['note'])}</p>"
                            "<p class='muted'>To connect, the operator "
                            "first registers Ventoryn's app in X's "
                            "developer console and sets the app "
                            "credentials — that step is not built here "
                            "and nothing is faked.</p>")
            elif kind == "facebook":
                if conn["status"] == "connected":
                    page_name = fb_raw.get("page_name") or ""
                    identity = (
                        f"<p class='muted'>Connected — Facebook Page "
                        f"{esc(page_name)}.</p>" if page_name else
                        "<p class='muted'>Connected — Facebook Page.</p>")
                    drafts = [d for d in store.list_social_drafts(site_id)
                              if d["kind"] == "facebook"]
                    draft_rows = "".join(
                        f"<div class='card'><p>{esc(d['text'])}</p>"
                        + (f"<p class='muted'>Link: {esc(d['link_url'])}"
                           "</p>" if d["link_url"] else "")
                        + (f"<p class='v-cost-line'>Cost if posted: "
                           f"{esc(rate_note('facebook'))}</p>"
                           if d["status"] == "pending" else "")
                        + f"<p>Status: <strong>{esc(d['status'])}</strong>"
                        + (f" (post {esc(d['tweet_id'])})"
                           if d["tweet_id"] else "") + "</p>"
                        + (f"<form method='post' action='/websites/sites/"
                           f"{esc(site_id)}/social/facebook/drafts/{esc(d['id'])}"
                           f"/approve'>{_totp_field(account)}<p>"
                           "<button class='btn btn-primary' type='submit'>"
                           "Approve &amp; post</button> "
                           "<button class='btn' type='submit' "
                           f"formaction='/websites/sites/{esc(site_id)}"
                           f"/social/facebook/drafts/{esc(d['id'])}/reject'>"
                           "Reject</button></p></form>"
                           if d["status"] == "pending" else "")
                        + "</div>"
                        for d in drafts) or (
                        "<p class='muted'>No drafts yet.</p>")
                    form = (identity +
                            "<form method='post' action='/websites/sites/"
                            f"{esc(site_id)}/social/facebook/drafts'>"
                            "<label for='facebook_text'>Draft a post</label>"
                            "<textarea id='facebook_text' name='text' maxlength="
                            "'2000' required></textarea>"
                            "<label for='facebook_link'>Link to sign (optional — "
                            "a URL on your own site)</label>"
                            "<input type='text' id='facebook_link' name='link_url' "
                            "placeholder='https://yoursite.com/...'>"
                            "<p><button class='btn' type='submit'>Save "
                            "draft</button></p></form>"
                            "<p class='muted'>Nothing is posted until you "
                            "click Approve &amp; post on a draft.</p>"
                            f"<h4>Drafts</h4>{draft_rows}")
                elif config.facebook_configured \
                        and config.app_registered("facebook"):
                    form = (f"<p class='muted'>{esc(conn['note'])}</p>"
                            f"<p><a class='btn btn-primary' href='/websites/"
                            f"sites/{esc(site_id)}/connections/facebook/start'>"
                            "Connect with Facebook</a></p>")
                else:
                    form = (f"<p class='muted'>{esc(conn['note'])}</p>"
                            "<p class='muted'>To connect, the operator "
                            "first registers Ventoryn's Meta app in the "
                            "provider's developer console and sets the app "
                            "credentials — that step is not built here "
                            "and nothing is faked.</p>")
            elif kind == "instagram":
                if conn["status"] == "connected":
                    identity = ("<p class='muted'>Connected — Instagram "
                                "Business account.</p>")
                    drafts = [d for d in store.list_social_drafts(site_id)
                              if d["kind"] == "instagram"]
                    draft_rows = "".join(
                        f"<div class='card'><p>{esc(d['text'])}</p>"
                        + (f"<p class='muted'>Link: {esc(d['link_url'])}"
                           "</p>" if d["link_url"] else "")
                        + (f"<p class='v-cost-line'>Cost if posted: "
                           f"{esc(rate_note('instagram'))}</p>"
                           if d["status"] == "pending" else "")
                        + f"<p>Status: <strong>{esc(d['status'])}</strong>"
                        + (f" (post {esc(d['tweet_id'])})"
                           if d["tweet_id"] else "") + "</p>"
                        + (f"<form method='post' action='/websites/sites/"
                           f"{esc(site_id)}/social/instagram/drafts/{esc(d['id'])}"
                           f"/approve'>{_totp_field(account)}<p>"
                           "<button class='btn btn-primary' type='submit'>"
                           "Approve &amp; post</button> "
                           "<button class='btn' type='submit' "
                           f"formaction='/websites/sites/{esc(site_id)}"
                           f"/social/instagram/drafts/{esc(d['id'])}/reject'>"
                           "Reject</button></p></form>"
                           if d["status"] == "pending" else "")
                        + "</div>"
                        for d in drafts) or (
                        "<p class='muted'>No drafts yet.</p>")
                    form = (identity +
                            "<form method='post' action='/websites/sites/"
                            f"{esc(site_id)}/social/instagram/drafts'>"
                            "<label for='instagram_text'>Draft a post</label>"
                            "<textarea id='instagram_text' name='text' maxlength="
                            "'2000' required></textarea>"
                            "<label for='instagram_link'>Link to sign (optional — "
                            "a URL on your own site)</label>"
                            "<input type='text' id='instagram_link' name='link_url' "
                            "placeholder='https://yoursite.com/...'>"
                            "<p><button class='btn' type='submit'>Save "
                            "draft</button></p></form>"
                            "<p class='muted'>Caption links are plain text, "
                            "not clickable — the link in a post is not "
                            "tappable. Nothing is posted until you "
                            "click Approve &amp; post on a draft.</p>"
                            f"<h4>Drafts</h4>{draft_rows}")
                elif config.facebook_configured \
                        and config.app_registered("instagram"):
                    form = (f"<p class='muted'>{esc(conn['note'])}</p>"
                            f"<p><a class='btn btn-primary' href='/websites/"
                            f"sites/{esc(site_id)}/connections/instagram/start'>"
                            "Connect with Instagram</a></p>")
                else:
                    form = (f"<p class='muted'>{esc(conn['note'])}</p>"
                            "<p class='muted'>To connect, the operator "
                            "first registers Ventoryn's Meta app in the "
                            "provider's developer console and sets the app "
                            "credentials — that step is not built here "
                            "and nothing is faked.</p>")
            else:
                # Reddit: no free API access exists for this anymore —
                # honest, permanent "not available" rather than a
                # half-built flow that can never actually connect.
                form = (f"<p class='muted'>{esc(conn['note'])}</p>"
                        "<p class='muted'>Reddit's API no longer offers "
                        "the free access this would need — this "
                        "connection is not available.</p>")
            conn_rows += (f"<div class='card'><h3>{esc(kind.replace('_', ' '))} "
                          f"{badge}</h3><p>{esc(conn['note'])}</p>"
                          f"<p>{esc(consent)}</p>"
                          f"<p class='muted'>Scopes requested: "
                          f"<code>{esc(scopes)}</code></p>"
                          f"{gate_html}{form}{disconnect_html}</div>")
        jobs = store.list_jobs(account["id"], site_id=site_id)
        job_rows = "".join(
            f"<tr><td>{esc(j['job_type'].replace('_', ' '))}</td>"
            f"<td>{esc(j['status'])}</td>"
            f"<td>{esc(job_cost_label(j, config.is_operator(account['email'])))}</td>"
            f"<td>{esc(j['result'] or '—')}</td></tr>"
            for j in jobs) or ("<tr><td colspan='4'>No jobs yet.</td></tr>")
        # Conversions from revenue_for_site: the three display
        # settings are already applied — amounts are stripped from the
        # rows when revenue is hidden, the rows are empty when details
        # are hidden. Attribution: Verified (signature reached the
        # payment), Pending (owner-recorded, no Stripe event yet),
        # Unattributed (real Stripe payment, no signature) — never
        # guessed. The signature tag is shown next to each attributed
        # sale, so the customer sees which link/action it came from.
        rev = store.revenue_for_site(site_id)
        if rev is not None and rev["conversions"]:
            def _attribution_label(c):
                if c.get("refunded_at"):
                    return "Refunded"
                if c.get("disputed_at"):
                    return "Disputed"
                if c.get("attribution") == "ventoryn":
                    return "Verified"
                if c.get("source") == "manual":
                    return "Pending"
                return "Unattributed"
            conv_rows = "".join(
                "<tr><td>"
                + (f"${c['amount_cents'] / 100:,.2f} "
                   f"{esc(c['currency'].upper())}"
                   if c.get("amount_cents") is not None else
                   "<span class='unavailable'>hidden</span>")
                + f"</td><td>{esc(c['source'])}</td>"
                f"<td>{_attribution_label(c)}</td>"
                f"<td>{esc(c.get('signature_tag') or '—')}</td>"
                f"<td>{esc(c['created_at'][:10])}</td></tr>"
                for c in rev["conversions"])
        elif rev is not None and not site.get("details_show", 1):
            conv_rows = ("<tr><td colspan='5'>Conversion details are "
                         "hidden — turn them on in Data &amp; privacy "
                         "controls above.</td></tr>")
        else:
            conv_rows = ("<tr><td colspan='5'>No paying customers "
                         "recorded yet.</td></tr>")

        def _chk(on):
            return "checked" if on else ""
        privacy_card = (f"<div class='card'><h2>Data &amp; privacy "
                        f"controls</h2>"
                        "<p class='muted'>Three separate choices. Revenue "
                        "amounts are private by default; the count and the "
                        "per-sale details are separate settings.</p>"
                        f"<form method='post' action='/websites/sites/{esc(site_id)}/privacy'>"
                        f"<input type='hidden' name='return_to' value="
                        f"'/websites/sites/{esc(site_id)}'>"
                        f"<label><input type='checkbox' name='revenue_show' "
                        f"{_chk(site.get('revenue_show'))}> Show my revenue "
                        f"on Ventoryn</label>"
                        f"<label><input type='checkbox' name='count_show' "
                        f"{_chk(site.get('count_show', 1))}> Show my "
                        f"customer count</label>"
                        f"<label><input type='checkbox' name='details_show' "
                        f"{_chk(site.get('details_show', 1))}> Show "
                        f"conversion details</label>"
                        f"{_totp_field(account)}"
                        "<p><button class='btn' type='submit'>Save privacy "
                        "settings</button></p></form></div>")
        signature_card = ("<div class='card'><h2>How attribution "
                          "works</h2>"
                          "<p>Every link Ventoryn creates for this site "
                          "carries the Ventoryn signature: "
                          "<code>utm_source=ventoryn</code> plus a signed "
                          "tag (e.g. <code>vtn_…</code>) that is recorded "
                          "when the link is created — which agent, which "
                          "job, when.</p>"
                          "<p>A sale counts as Ventoryn-attributed only "
                          "when that tag reaches your checkout (it must be "
                          "in the Stripe metadata). Use the Ventoryn links "
                          "and the tag follows to checkout. A sale without "
                          "the tag is reported as unattributed — never "
                          "guessed.</p></div>")
        balance_text = ("Unlimited"
                        if config.is_operator(account["email"])
                        else str(account["credits_balance"]))
        from ventoryn import site_snapshot as _snap
        snap = _snap.snapshot_store_for(store).get(site["id"])
        if snap is None:
            # Sites added before the snapshot existed never had one:
            # start it now (start() enforces one run at a time), so the
            # "Looking at your site…" card is never a dead end.
            _snap.start(store, site, REPORTS_DIR)
            snap = _snap.snapshot_store_for(store).get(site["id"])
        snapshot_html = (f"<style>{_snap.SNAPSHOT_CSS}</style>"
                         + _snap.render_card(site, snap))
        # Founding Customer Program: verified customers are distinct,
        # real Stripe payments (manual sales never count as verified).
        f100 = first100_summary(store, site_id)
        if site.get("count_show", 1):
            founding_card = (f"<div class='card'><h2>Founding Customer "
                "Program</h2>"
                f"<p><strong>{f100['verified']} / 100</strong> verified "
                f"customers — {f100['remaining']} to go.</p>"
                f"<p class='muted'>{f100['paying']} paying customers "
                f"recorded · {f100['attributed']} Ventoryn-attributed · "
                f"{f100['unattributed']} unattributed.</p>"
                "<p class='muted'>Verified means a distinct customer "
                "with a real Stripe payment. Owner-recorded manual "
                "sales do not count as verified.</p></div>")
        else:
            founding_card = ("<div class='card'><h2>Founding Customer "
                "Program</h2><p class='muted'>Your customer count is "
                "hidden — turn it on in Data &amp; privacy controls "
                "below.</p></div>")
        inner = snapshot_html + _va_card(site, va_check) + f"""<div class='card'><h2>Funnel — visitors to paying customers</h2>
<div class='table-wrap'><table>{funnel}</table></div>
<p class='muted'>Every number here comes from a real source. Where a
connection is missing the value reads "unavailable" — never invented.</p></div>
{founding_card}
<h2>Run agent work</h2>
{_autopilot_card(site)}
{_video_settings_card(site)}
<div class='card'><p>Credit balance: <strong>{balance_text}</strong></p>
<form method='post' action='/websites/sites/{esc(site_id)}/jobs' enctype='multipart/form-data' class='v-jobform'>
<label for='job_type'>Job</label>
<select id='job_type' name='job_type'>{_job_options()}</select>
<div class='v-job-field' data-jobs='community_listening seo_article'>
<label for='job_keywords'>Keywords <span class='muted'>(comma-separated — required for community listening, optional for SEO)</span></label>
<input id='job_keywords' name='keywords' type='text' maxlength='300' placeholder='e.g. cissp exam, security certification'></div>
<div class='v-job-field' data-jobs='community_listening'>
<label for='job_community'>Communities <span class='muted'>(optional — e.g. r/cissp, r/cybersecurity; all of Reddit if empty)</span></label>
<input id='job_community' name='community' type='text' maxlength='300' placeholder='r/cissp, r/cybersecurity'></div>
<div class='v-job-field' data-jobs='social_report'>
<label for='job_export'>Social export file <span class='muted'>(CSV or JSON, up to 5 MB)</span></label>
<input id='job_export' name='export' type='file' accept='.csv,.json,text/csv,application/json'></div>
{_video_fields(site)}
<p id='job_hint' class='muted'></p>
<p><button class='btn btn-primary' type='submit'>Run job</button></p></form>
<p class='muted'>Nothing is published or posted without your approval.</p></div>
<script>
(function () {{
  var sel = document.getElementById('job_type');
  var hint = document.getElementById('job_hint');
  if (!sel) return;
  function sync() {{
    var job = sel.value;
    document.querySelectorAll('.v-job-field').forEach(function (el) {{
      el.hidden = (el.getAttribute('data-jobs') || '').split(' ').indexOf(job) < 0;
    }});
    var opt = sel.options[sel.selectedIndex];
    hint.textContent = (opt && opt.getAttribute('data-hint')) || '';
  }}
  sel.addEventListener('change', sync);
  sync();
}})();
</script>
<h2>Jobs for this site</h2>
<div class='table-wrap'><table>
<tr><th>Job</th><th>Status</th><th>Cost</th><th>Result</th></tr>
{job_rows}</table></div>
<h2>Paying customers</h2>
<div class='table-wrap'><table>
<tr><th>Amount</th><th>Source</th><th>Attribution</th><th>Signature</th><th>Date</th></tr>
{conv_rows}</table></div>
<div class='card'><h3>Record a sale manually</h3>
<p class='muted'>If a customer paid outside Stripe, record it here so the
funnel counts it. This is the owner-recorded fallback.</p>
<form method='post' action='/websites/sites/{esc(site_id)}/conversions'>
<label for='amount'>Amount (USD)</label>
<input type='text' id='amount' name='amount_usd' placeholder='19.00'>
<p><button class='btn' type='submit'>Record sale</button></p></form></div>
<h2>Connections</h2>{conn_rows}
{privacy_card}
{signature_card}"""
        return _signed_page(site["url"], inner, account, current="websites")

    @app.post("/websites/sites/{site_id}/connections/{kind}")
    def connection_update(request: Request, site_id: str, kind: str,
                          site_domain: str = Form(""),
                          api_key: str = Form(""),
                          webhook_secret: str = Form(""),
                          return_to: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_signed_page("Website", "<p>Website not found.</p>",
                                      account, current="websites"), status_code=404)
        if kind == "reddit":
            return HTMLResponse(
                _signed_page(site["url"],
                      "<p>This connection is not available — Reddit's API "
                      "no longer offers the free access this would need. "
                      "No account was connected and nothing was stored.</p>"
                      f"<p><a href='/websites/sites/{esc(site_id)}'>Back</a>.</p>",
                      account, current="websites"),
                status_code=400)
        if kind in ("x", "facebook", "instagram"):
            # OAuth connections use the Connect button on this page,
            # not this form.
            label = {"x": "X", "facebook": "Facebook",
                     "instagram": "Instagram"}[kind]
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>Use the \"Connect with {label}\" link to "
                      "connect this account.</p>"
                      f"<p><a href='/websites/sites/{esc(site_id)}'>Back</a>.</p>",
                      account, current="websites"),
                status_code=400)
        if kind == "plausible":
            raw = (store.get_connection(site_id, "plausible") or {}
                   ).get("_detail_raw") or {}
            domain = (site_domain or "").strip() or site["host"]
            detail = dict(raw)
            detail["site_domain"] = domain
            if api_key.strip():
                detail["api_key"] = api_key.strip()
                store.update_connection(
                    site_id, "plausible", "connected",
                    "Connected — showing real visitors from Plausible.",
                    detail)
            else:
                store.update_connection(
                    site_id, "plausible", "not_connected",
                    "Not connected — add your Plausible API key to see real "
                    "visitors here.", detail)
        elif kind == "stripe_sales":
            raw = (store.get_connection(site_id, "stripe_sales") or {}
                   ).get("_detail_raw") or {}
            detail = dict(raw)
            if webhook_secret.strip():
                detail["webhook_secret"] = webhook_secret.strip()
                store.update_connection(
                    site_id, "stripe_sales", "connected",
                    "Connected — sales from your Stripe webhook are counted "
                    "as paying customers for this site.", detail)
            else:
                store.update_connection(
                    site_id, "stripe_sales", "not_connected",
                    "Not connected — add your Stripe webhook signing secret "
                    "to see real paying customers here.", detail)
        else:
            return HTMLResponse(
                _signed_page(site["url"], "<p>Unknown connection.</p>", account, current="websites"),
                status_code=400)
        return RedirectResponse(_integrations_return(return_to)
                                or f"/websites/sites/{site_id}",
                                status_code=303)

    @app.post("/websites/sites/{site_id}/privacy")
    def site_privacy(request: Request, site_id: str,
                     return_to: str = Form(""),
                     revenue_show: str = Form(""),
                     count_show: str = Form(""),
                     details_show: str = Form(""),
                     totp_code: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_signed_page("Website", "<p>Website not found.</p>",
                                      account, current="websites"), status_code=404)
        ok, err = _fresh_totp_ok(account, totp_code)
        if not ok:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(err)}</p><p><a href='/websites/sites/{esc(site_id)}'>"
                      "Back</a>.</p>", account, current="websites"),
                status_code=400)
        # A checkbox sends "on" when ticked and nothing at all when
        # unticked — the three settings are independent.
        store.set_revenue_show(site_id, revenue_show == "on")
        store.set_count_show(site_id, count_show == "on")
        store.set_details_show(site_id, details_show == "on")
        dest = (return_to if return_to.startswith("/account/")
                else f"/websites/sites/{site_id}")
        return RedirectResponse(dest, status_code=303)

    @app.post("/websites/sites/{site_id}/connections/{kind}/disconnect")
    def connection_disconnect(request: Request, site_id: str, kind: str,
                              totp_code: str = Form(""),
                              return_to: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_signed_page("Website", "<p>Website not found.</p>",
                                      account, current="websites"), status_code=404)
        ok, err = _fresh_totp_ok(account, totp_code)
        if not ok:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(err)}</p><p><a href='/websites/sites/{esc(site_id)}'>"
                      "Back</a>.</p>", account, current="websites"),
                status_code=400)
        conn = store.get_connection(site_id, kind) or {}
        raw = conn.get("_detail_raw") or {}
        client_id = config.x_client_id if kind == "x" else ""
        client_secret = config.x_client_secret if kind == "x" else ""
        _revoked, note = revoke_provider_token(
            kind, raw, client_id=client_id, client_secret=client_secret)
        store.disconnect_connection(site_id, kind)
        back = _integrations_return(return_to)
        if back:
            # Disconnected from an Integrations window: back to that app.
            return RedirectResponse(back, status_code=303)
        inner = (f"<p>{esc(kind.replace('_', ' '))} is disconnected. "
                 f"{esc(note)}</p>"
                 f"<p><a href='/websites/sites/{esc(site_id)}'>Back to "
                 f"{esc(site['url'])}</a>.</p>")
        return HTMLResponse(_signed_page(site["url"], inner, account, current="websites"))

    def _job_options() -> str:
        """Every job, with its credit cost; a job this server cannot run
        yet says why (running it then charges nothing)."""
        out = []
        for jt, label in site_jobs.JOB_LABELS.items():
            cost = CREDIT_COSTS[jt]
            blocker = site_jobs.server_blocker(jt)
            note = f" — {blocker}" if blocker else ""
            hint = site_jobs.JOB_HINTS.get(jt, "")
            out.append(f"<option value='{esc(jt)}' data-hint='{esc(hint)}'>"
                       f"{esc(label)} — {cost} credits{esc(note)}</option>")
        return "".join(out)

    def _video_settings_card(site: dict) -> str:
        """This website's voice (ElevenLabs) and presenter (HeyGen) —
        so each brand has its own face and voice."""
        from core import media_providers as mp

        voices, avatars = mp.list_voices(), mp.list_avatars()
        if not voices and not avatars:
            return ""
        cur_v, cur_a = site.get("video_voice_id") or "", site.get("video_avatar_id") or ""

        def opt(value, label, cur):
            sel = " selected" if value == cur else ""
            return f"<option value='{esc(value)}'{sel}>{esc(label)}</option>"
        v_opts = opt("", "Server default", cur_v) + "".join(
            opt(v["id"], v["name"], cur_v) for v in voices)
        a_opts = (opt("", "Server default", cur_a) + opt("none", "No presenter", cur_a)
                  + "".join(opt(a["id"], a["name"] + (" (yours)" if a["private"] else ""), cur_a)
                            for a in avatars))
        return (f"<div class='card' id='video-settings'><h3>Video presenter and voice</h3>"
                f"<p class='muted'>Who presents and who speaks in this website's videos. "
                f"Each brand can have its own.</p>"
                f"<form method='post' action='/websites/sites/{esc(site['id'])}/video-settings'>"
                + (f"<label>Voice (ElevenLabs)<select name='voice_id'>{v_opts}</select></label>"
                   if voices else "<input type='hidden' name='voice_id' value=''>")
                + (f"<label>Presenter (HeyGen)<select name='avatar_id'>{a_opts}</select></label>"
                   if avatars else "<input type='hidden' name='avatar_id' value=''>")
                + "<p><button class='btn' type='submit'>Save</button></p></form></div>")

    @app.get("/websites/sites/{site_id}/videos/{job_id}")
    def video_file(request: Request, site_id: str, job_id: str, download: str = ""):
        """Play or download an agent-made video. Owner only; the file must
        sit in the reports' videos folder (ventoryn/videos.py)."""
        from fastapi.responses import FileResponse
        from ventoryn.videos import video_path

        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        job = store.get_job(job_id)
        site = store.get_site(site_id)
        if (job is None or site is None or job["site_id"] != site_id
                or site["account_id"] != account["id"]):
            raise HTTPException(404, "video not found")
        path = video_path(job, REPORTS_DIR)
        if path is None:
            raise HTTPException(404, "video not found")
        return FileResponse(path, media_type="video/mp4",
                            filename=path.name if download else None,
                            content_disposition_type="attachment" if download else "inline")

    @app.post("/websites/sites/{site_id}/videos/{job_id}/review")
    def video_review(request: Request, site_id: str, job_id: str, decision: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        job = store.get_job(job_id)
        site = store.get_site(site_id)
        if (job is None or site is None or job["site_id"] != site_id
                or site["account_id"] != account["id"] or job["job_type"] != "video_render"):
            raise HTTPException(404, "video not found")
        if decision not in ("approve", "reject"):
            raise HTTPException(400, "invalid decision")
        store.set_video_review(job_id, account["id"],
                               "approved" if decision == "approve" else "rejected")
        return RedirectResponse(f"/approvals#video-{job_id}", status_code=303)

    @app.post("/websites/sites/{site_id}/video-settings")
    def site_video_settings(request: Request, site_id: str,
                            voice_id: str = Form(""), avatar_id: str = Form("")):
        import re as _re

        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            raise HTTPException(404, "website not found")
        ok = lambda v: v == "" or v == "none" or bool(_re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v))
        if not (ok(voice_id) and ok(avatar_id)) or voice_id == "none":
            raise HTTPException(400, "invalid choice")
        store.set_site_video(site_id, voice_id, avatar_id)
        return RedirectResponse(f"/websites/sites/{site_id}#video-settings", status_code=303)

    def _autopilot_card(site: dict) -> str:
        from ventoryn import autopilot

        on = bool(int(site.get("autopilot", 1) or 0))
        plan = ", ".join(
            f"{site_jobs.JOB_LABELS[jt].split(' — ')[0]} every {days} days"
            for jt, days in autopilot.AUTOPILOT_JOBS)
        from ventoryn.nav import autopilot_switch_html

        return (f"<div class='card' id='autopilot'><div class='v-autopilot-head'>"
                f"<h3>Agents on autopilot</h3>"
                f"{autopilot_switch_html(site['id'], on, site['host'] or site['url'])}</div>"
                f"<p class='muted'>When on, the agents work on this website by themselves: "
                f"{esc(plan)}. Each run costs the same credits as running it by hand; "
                f"nothing is published without your approval.</p>"
                "<style>.v-autopilot-head{display:flex;align-items:center;"
                "justify-content:space-between;gap:12px}"
                ".v-autopilot-head h3{margin:0}</style></div>")

    # -- campaigns: approve (2FA) / reject / remove a post ---------------------
    app.state.totp_field = _totp_field

    def _own_campaign(account, campaign_id):
        c = store.get_campaign(campaign_id)
        return c if c and c["account_id"] == account["id"] else None

    @app.post("/campaigns/{campaign_id}/approve")
    def campaign_approve(request: Request, campaign_id: str,
                         totp_code: str = Form("")):
        from ventoryn import campaigns as _camp

        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        c = _own_campaign(account, campaign_id)
        if c is None or c["status"] != "proposed":
            raise HTTPException(404, "campaign not found")
        ok, err = _fresh_totp_ok(account, totp_code)
        if not ok:
            return HTMLResponse(_signed_page(
                "Approvals", f"<p>{esc(err)}</p><p><a href='/approvals#campaign-"
                f"{esc(campaign_id)}'>Back</a>.</p>", account, current="approvals"),
                status_code=400)
        _camp.schedule(store, c)
        return RedirectResponse(f"/approvals?campaign={campaign_id}", status_code=303)

    @app.post("/campaigns/{campaign_id}/reject")
    def campaign_reject(request: Request, campaign_id: str):
        from ventoryn import campaigns as _camp

        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        c = _own_campaign(account, campaign_id)
        if c is None or c["status"] != "proposed":
            raise HTTPException(404, "campaign not found")
        _camp.reject(store, c)
        return RedirectResponse("/approvals", status_code=303)

    @app.post("/campaigns/{campaign_id}/posts/{draft_id}/remove")
    def campaign_remove_post(request: Request, campaign_id: str, draft_id: str):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        c = _own_campaign(account, campaign_id)
        d = store.get_social_draft(draft_id)
        if c is None or d is None or d.get("campaign_id") != campaign_id:
            raise HTTPException(404, "post not found")
        store.remove_campaign_draft(draft_id)
        return RedirectResponse(f"/approvals#campaign-{campaign_id}", status_code=303)

    @app.get("/websites/sites/{site_id}/articles/{job_id}", response_class=HTMLResponse)
    def article_view(request: Request, site_id: str, job_id: str):
        """Read an SEO article draft the agents wrote for this website.
        Only the owner's own finished seo_article job; the file must sit
        inside this website's draft folder."""
        from ventoryn.reports import render_markdown, report_path

        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        job = store.get_job(job_id)
        if (site is None or site["account_id"] != account["id"] or job is None
                or job["site_id"] != site_id or job["job_type"] != "seo_article"
                or job["status"] != "done"):
            raise HTTPException(404, "article not found")
        import re as _re
        m = _re.search(r"report (\S+\.md)", job["result"] or "")
        report = report_path(m.group(1)) if m else None
        text = report.read_text(encoding="utf-8", errors="replace") if report else ""
        dm = _re.search(r"- Draft: `([^`]+)`", text)
        if not dm:
            raise HTTPException(404, "article not found")
        draft = Path(dm.group(1)).resolve()
        folder = (REPORTS_DIR / "seo").resolve()
        if folder not in draft.parents or not draft.is_file():
            raise HTTPException(404, "article not found")
        body = render_markdown(draft.read_text(encoding="utf-8", errors="replace"))
        return HTMLResponse(_signed_page(
            f"Article draft — {site['host']}",
            "<p class='muted'>A draft the agents wrote. Nothing was published — "
            "copy it to your site when you are happy with it.</p>"
            f"<article class='card v-doc'>{body}</article>"
            "<p><a href='/approvals'>Back to Approvals</a></p>",
            account, current="approvals"))

    @app.post("/websites/sites/{site_id}/autopilot")
    def site_autopilot(request: Request, site_id: str, on: str = Form("1"),
                       return_to: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_signed_page("Website", "<p>Website not found.</p>",
                                      account, current="websites"), status_code=404)
        store.set_site_autopilot(site_id, on == "1")
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"on": on == "1"})
        # Only back to the Websites list or this site's page — never
        # anywhere a crafted form names.
        back = "/portfolio" if return_to == "/portfolio" else f"/websites/sites/{site_id}#autopilot"
        return RedirectResponse(back, status_code=303)

    def _video_fields(site: dict) -> str:
        """Video job options: ElevenLabs voiceover and HeyGen presenter,
        each available only when the server has its keys and the
        website has a voice / presenter."""
        from core import media_providers as mp

        vb, pb = mp.voice_blocker(), mp.presenter_blocker()
        avatar = site.get("video_avatar_id") or ""
        if avatar == "none":
            pb = "no presenter for this website (see Video presenter and voice)"
        elif avatar and pb == "needs HEYGEN_AVATAR_ID on the server":
            pb = None
        if site.get("video_voice_id") and vb == "needs ELEVENLABS_VOICE_ID on the server":
            vb = None
        voice = (f"<label><input type='checkbox' name='voiceover' value='on' checked> "
                 f"Voiceover (ElevenLabs)</label>" if not vb else
                 f"<p class='muted'>Voiceover (ElevenLabs): {esc(vb)}.</p>")
        presenter = (f"<label><input type='checkbox' name='presenter' value='heygen'> "
                     f"Talking presenter (HeyGen) — makes the whole video</label>" if not pb else
                     f"<p class='muted'>Presenter (HeyGen): {esc(pb)}.</p>")
        return (f"<div class='v-job-field' data-jobs='video_render'>{voice}{presenter}"
                f"<p class='muted'>Both are paid APIs; their usage is added to this "
                f"website's Costs.</p></div>")

    @app.post("/websites/sites/{site_id}/jobs")
    async def run_job(request: Request, site_id: str,
                      job_type: str = Form(...),
                      keywords: str = Form(""), community: str = Form(""),
                      voiceover: str = Form("off"), presenter: str = Form(""),
                      export: UploadFile | None = File(None)):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_signed_page("Website", "<p>Website not found.</p>",
                                      account, current="websites"), status_code=404)
        cost = CREDIT_COSTS.get(job_type)
        if cost is None:
            return HTMLResponse(
                _signed_page(site["url"], "<p>Unknown job type.</p>", account, current="websites"),
                status_code=400)
        inputs = {"keywords": keywords[:300], "community": community[:300],
                  "avatar_id": site.get("video_avatar_id") or "",
                  "voiceover": "on" if voiceover == "on" else "off",
                  "presenter": "heygen" if presenter == "heygen" else ""}
        if export is not None and job_type == "social_report":
            inputs["export_bytes"] = await export.read(
                site_jobs.MAX_UPLOAD_BYTES + 1)
            inputs["export_name"] = export.filename or "export.csv"
        try:
            site_jobs.preflight(job_type, inputs)
        except (site_jobs.JobInputError, site_jobs.JobUnavailable) as exc:
            code = 503 if isinstance(exc, site_jobs.JobUnavailable) else 400
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(exc)}</p>"
                      f"<p><a href='/websites/sites/{esc(site_id)}'>Back</a>.</p>",
                      account, current="websites"),
                status_code=code)
        is_op = config.is_operator(account["email"])
        balance = store.credit_balance(account["id"])
        if not is_op and balance < cost:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>That job costs {cost} credits and the balance is "
                      f"{balance}. Nothing ran and nothing was charged. "
                      f"Top up by subscribing on the "
                      f"<a href='/account/subscribe'>Subscribe</a> page.</p>",
                      account, current="websites"),
                status_code=402)
        # Charge first (atomic — the job row is created with the charge),
        # run second; a failed run is refunded, so nobody pays for work
        # that did not happen.
        try:
            job = store.charge_credits(account["id"], job_type, site_id,
                                       is_operator=is_op)
        except InsufficientCredits as exc:
            return HTMLResponse(
                _signed_page(site["url"],
                      f"<p>{esc(exc)}</p><p><a href='/websites/sites/"
                      f"{esc(site_id)}'>Back</a>.</p>", account, current="websites"),
                status_code=402)
        try:
            import asyncio

            orchestrator = app.state.ecosystem["orchestrator"]
            # The work is blocking (HTTP fetches, LLM calls, ffmpeg):
            # run it off the event loop.
            summary, _report = await asyncio.to_thread(
                site_jobs.run, job_type, site, inputs,
                orchestrator=orchestrator, reports_dir=REPORTS_DIR, store=store)
            store.set_job_result(job["id"], "done", summary)
            # The admin's recent-activity feed: a real job ran.
            store.record_activity_event(
                account["id"], "job",
                f"{site_jobs.JOB_LABELS[job_type].split(' — ')[0]} ran on "
                f"{site['url']}: {summary.split(' · report')[0]}")
        except Exception as exc:  # noqa: BLE001 - refund and say so
            # Only refund what was actually charged: the operator's job
            # charged 0, so there is nothing to give back and no ledger
            # entry to write. Customers keep the exact message as before.
            charged = job["credits_charged"]
            if charged:
                store.grant_credits(account["id"], charged,
                                    f"Refund — {job_type} job failed")
                note = f"Failed — refunded {charged} credits ({exc})"
                detail = (f"<p>The job failed ({esc(exc)}) and the {charged} "
                          f"credits were refunded.</p>")
            else:
                note = f"Failed ({exc})"
                detail = (f"<p>The job failed ({esc(exc)}). Nothing was "
                          f"charged.</p>")
            store.set_job_result(job["id"], "failed", note)
            return HTMLResponse(
                _signed_page(site["url"],
                      detail +
                      f"<p><a href='/websites/sites/{esc(site_id)}'>Back</a>.</p>",
                      account, current="websites"),
                status_code=500)
        return RedirectResponse(f"/websites/sites/{site_id}", status_code=303)

    def _va_card(site: dict, check: str) -> str:
        """Ventoryn Analytics: the snippet, live status, install check."""
        from ventoryn.analytics import analytics_for, snippet

        va = analytics_for(store).summary(site["id"])
        if va["installed"]:
            status = (f"<p><span class='badge badge-ok'>Receiving data</span> "
                      f"Last event {esc(va['last_seen'][:16].replace('T', ' '))} UTC · "
                      f"{va['visitors']:,} visitors and {va['pageviews']:,} "
                      f"page views in the last 30 days"
                      + (f" · {va['signed_visits']:,} through signed "
                         f"Ventoryn links" if va["signed_visits"] else "")
                      + "</p>")
            top = "".join(f"<tr><td>{esc(src)}</td><td>{v:,}</td></tr>"
                          for src, v in va["sources"][:8])
            if top:
                status += ("<div class='table-wrap'><table><tr><th>Source"
                           "</th><th>Visitors (30 days)</th></tr>"
                           f"{top}</table></div>")
        else:
            status = ("<p><span class='badge badge-off'>Not installed</span> "
                      "No visits recorded yet.</p>")
        notice = ""
        if check:
            ok, _, message = check.partition(":")
            kind = "ok" if ok == "ok" else "warn"
            notice = (f"<div class='v-notice v-notice-{kind}'>"
                      f"{esc(message)}</div>")
        install = ""
        if not va["installed"]:
            code = esc(snippet(site["id"], config.base_url))
            install = (
                f"<p>Add this line inside <code>&lt;head&gt;</code> on every "
                f"page of {esc(site['host'])}:</p>"
                f"<pre class='v-snippet'><code>{code}</code></pre>"
                f"<p class='muted'>Count signups by calling "
                f"<code>ventoryn('signup')</code> when someone signs up. "
                f"No cookies, nothing stored on visitors' devices, no "
                f"personal data kept. If your site sends a "
                f"Content-Security-Policy, allow "
                f"<code>{esc(config.base_url)}</code> in "
                f"<code>script-src</code> and <code>connect-src</code>.</p>"
                f"<form method='post' action='/websites/sites/{esc(site['id'])}"
                f"/analytics/check'><button class='btn' type='submit'>"
                f"Check my installation</button></form>")
        return (f"<div class='card' id='ventoryn-analytics'>"
                f"<h2>Ventoryn Analytics</h2>{notice}{status}{install}</div>")

    @app.post("/websites/sites/{site_id}/snapshot")
    def snapshot_refresh(request: Request, site_id: str):
        from ventoryn import site_snapshot

        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_signed_page("Website", "<p>Website not found.</p>",
                                      account, current="websites"), status_code=404)
        # Cooldown + one-at-a-time are enforced inside start().
        site_snapshot.start(store, site, REPORTS_DIR)
        return RedirectResponse(f"/websites/sites/{site_id}#site-snapshot",
                                status_code=303)

    @app.get("/websites/sites/{site_id}/snapshot-image")
    def snapshot_image(request: Request, site_id: str):
        """The owner's own site screenshot — owner only, never public."""
        from fastapi.responses import FileResponse, Response

        from ventoryn.site_screenshot import snapshot_image_path

        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return Response(status_code=404)
        path = snapshot_image_path(REPORTS_DIR, site_id)
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(str(path), media_type="image/png",
                            headers={"Cache-Control": "private, max-age=300"})

    @app.post("/websites/sites/{site_id}/analytics/check")
    async def va_check_install(request: Request, site_id: str):
        import asyncio
        import urllib.parse as _up

        from ventoryn.analytics import check_install

        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_signed_page("Website", "<p>Website not found.</p>",
                                      account, current="websites"), status_code=404)
        ok, message = await asyncio.to_thread(check_install, site)
        q = _up.quote(("ok:" if ok else "no:") + message, safe="")
        return RedirectResponse(
            f"/websites/sites/{site_id}?va_check={q}#ventoryn-analytics",
            status_code=303)

    @app.post("/websites/sites/{site_id}/conversions")
    def record_sale(request: Request, site_id: str,
                    amount_usd: str = Form(...)):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_signed_page("Website", "<p>Website not found.</p>",
                                      account, current="websites"), status_code=404)
        try:
            amount_cents = int(round(float(amount_usd) * 100))
            if amount_cents <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return HTMLResponse(
                _signed_page(site["url"],
                      "<p>Enter a sale amount in dollars, e.g. 19.00.</p>"
                      f"<p><a href='/websites/sites/{esc(site_id)}'>Back</a>.</p>",
                      account, current="websites"),
                status_code=400)
        store.record_conversion(site_id, amount_cents, "usd", "manual")
        return RedirectResponse(f"/websites/sites/{site_id}", status_code=303)

    def _count_own_sale(event: dict) -> None:
        """A Ventoryn subscription is also a paying customer of
        ventoryn.app itself. Its events are signed with the platform
        secret (Ventoryn's own Stripe account), so they never reach the
        per-site sales secrets — count them for the own site here. Only
        the own site id is honoured, and only from a platform-verified
        event; the derived event id keeps redeliveries counted once."""
        from ventoryn.analytics import own_site_id

        own = own_site_id()
        obj = (event.get("data") or {}).get("object") or {}
        if (not own or ((obj.get("metadata") or {}).get("ventoryn_site_id") != own)
                or store.get_site(own) is None):
            return
        handle_customer_sale_webhook(
            store, own, {**event, "id": f"{event['id']}:own-sale"})

    # -- Stripe webhook (no login — the signature is the auth) ---------------
    @app.post("/account/billing/webhook")
    async def stripe_webhook(request: Request):
        payload = await request.body()
        header = request.headers.get("stripe-signature", "")
        try:
            event = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(event, dict) or not event.get("id"):
            return JSONResponse({"error": "invalid event"}, status_code=400)
        if config.stripe_webhook_secret and verify_webhook_signature(
                payload, header, config.stripe_webhook_secret):
            result = handle_platform_webhook(store, config, event)
            _count_own_sale(event)
            return JSONResponse({"received": True, "result": result})
        for site_id, secret in store.stripe_sales_secrets():
            if verify_webhook_signature(payload, header, secret):
                result = handle_customer_sale_webhook(store, site_id, event)
                return JSONResponse({"received": True, "result": result})
        return JSONResponse({"error": "invalid signature"}, status_code=400)
