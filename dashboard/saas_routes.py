"""Customer-facing SaaS routes for Ventoryn (/account/...).

Registered by dashboard.app.create_app() — the shell lives on the same
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
  GET  /account/sites/new               add a website
  POST /account/sites                   create the site
  GET  /account/sites/{site_id}         site detail: funnel, connections, jobs
  POST /account/sites/{site_id}/connections/{kind}
                                         connect Plausible / Stripe sales
  POST /account/sites/{site_id}/jobs    run a job (analyze today)
  POST /account/sites/{site_id}/conversions
                                         owner-recorded sale (fallback)

Sign-in flow (the ShipItAyiti flow): the landing page carries the
sign-in and the URL box; after Google sign-in EVERYONE goes straight
to Live Ops (/ops), scoped to their account. A website typed on the
landing page rides along (?site_url=…) so /ops can offer the
subscribe / add-site step for it. /admin is a separate URL for the
operator only (see dashboard.app).

Nothing here publishes or posts anywhere. A job that costs more
credits than the balance is refused with the balance shown.
"""

from __future__ import annotations

import html
import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.saas import (
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
    exchange_google_code,
    google_auth_url,
    google_userinfo,
    handle_customer_sale_webhook,
    handle_platform_webhook,
    plausible_visitors,
    slug_for_url,
    validate_site_url,
    verify_webhook_signature,
    create_checkout_session,
)

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

_SAAS_CSS = """
body { font-family: sans-serif; margin: 0 auto; max-width: 720px;
       padding: 16px; color: #222; background: #fff; }
h1 { font-size: 24px; } h2 { font-size: 19px; margin-top: 28px; }
.muted { color: #666; font-size: 14px; }
.unavailable { color: #666; font-style: italic; }
.card { border: 1px solid #ddd; border-radius: 12px; padding: 16px;
        margin: 12px 0; background: #fff; }
.card h2, .card h3 { margin-top: 0; }
.big { font-size: 28px; font-weight: bold; }
.btn { display: inline-block; min-height: 44px; line-height: 44px;
       padding: 0 20px; margin: 4px 4px 4px 0; border-radius: 8px;
       border: 1px solid #ccc; background: #fff; color: #222;
       font-size: 16px; cursor: pointer; text-decoration: none;
       text-align: center; box-sizing: border-box; }
.btn-primary { background: #c4b5fd; border-color: #c4b5fd; color: #111;
               font-weight: bold; }
.btn-google { background: #fff; border-color: #888; }
label { display: block; margin: 12px 0 4px; font-weight: bold; }
input[type=text], input[type=url], select {
       width: 100%; min-height: 44px; padding: 8px 10px; font-size: 16px;
       border: 1px solid #ccc; border-radius: 8px; box-sizing: border-box; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; }
th, td { border: 1px solid #ddd; padding: 8px; text-align: left;
         font-size: 14px; vertical-align: top; }
th { background: #f0f0f0; }
.table-wrap { overflow-x: auto; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
         font-size: 12px; background: #eef2ff; border: 1px solid #c7d2fe; }
.badge-off { background: #f3f4f6; border-color: #d1d5db; color: #666; }
.badge-ok { background: #e6f4ea; border-color: #34a853; }
@media (max-width: 560px) {
  body { padding: 12px; } h1 { font-size: 21px; }
  th, td { font-size: 13px; padding: 6px; }
  .btn { width: 100%; margin: 4px 0; }
}
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, inner: str, account: dict | None = None) -> str:
    # The customer nav — their own pages only: Live Ops, My
    # Portfolio, Approvals, Report, My account, Subscribe, Sign out.
    # Never an operator tool, never another account's data.
    nav = ("<nav class='customer-nav' aria-label='Ventoryn'>"
           "<a href='/ops'>Live Ops</a>"
           "<a href='/portfolio'>My Portfolio</a>"
           "<a href='/approvals'>Approvals</a>"
           "<a href='/report'>Report</a>"
           "<a href='/account/dashboard'>My account</a>"
           "<a href='/account/subscribe'>Subscribe</a>"
           "<a href='/account/logout'>Sign out</a></nav>")
    return (f"<html><head><title>{esc(title)} — Ventoryn</title>"
            f"<meta name='viewport' content='width=device-width,"
            f" initial-scale=1'><style>{_SAAS_CSS}{_NAV_CSS}</style></head>"
            f"<body>{nav}<p><a href='/'>Ventoryn</a></p>"
            f"<h1>{esc(title)}</h1>{inner}</body></html>")


_NAV_CSS = """
.customer-nav { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 16px; }
.customer-nav a { display: inline-block; min-height: 44px; line-height: 44px;
  padding: 0 14px; border-radius: 8px; background: #f3efff;
  border: 1px solid #c4b5fd; color: #111; text-decoration: none;
  font-size: 15px; }
.customer-nav a:hover { background: #e9e2ff; }
"""


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
    """Visitors / signups / paying customers — each from a real source
    or the word 'unavailable'. Nothing here is invented."""
    if plausible_key:
        detail = store.get_connection(site["id"], "plausible") or {}
        raw = detail.get("_detail_raw") or {}
        domain = raw.get("site_domain") or site["host"]
        visitors = plausible_visitors(plausible_key, domain)
        visitors_html = (f"{visitors:,}" if visitors is not None else
                         "<span class='unavailable'>unavailable — "
                         "Plausible did not answer</span>")
    else:
        visitors_html = ("<span class='unavailable'>unavailable — "
                         "connect Plausible below</span>")
    paying = store.paying_customer_count(site["id"])
    connections = {c["kind"]: c for c in store.get_connections(site["id"])}
    stripe = connections.get("stripe_sales") or {}
    if stripe.get("status") == "connected":
        paying_html = f"{paying:,}"
    else:
        paying_html = ("<span class='unavailable'>unavailable — "
                       "connect your Stripe webhook below</span>")
    return (
        f"<tr><td>Visitors (last 30 days)</td><td>{visitors_html}</td></tr>"
        f"<tr><td>Signups</td><td><span class='unavailable'>unavailable — "
        f"signup tracking is not connected yet</span></td></tr>"
        f"<tr><td>Paying customers</td><td>{paying_html}</td></tr>")


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
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie(PENDING_SITE_COOKIE, path="/")
        response.set_cookie(SESSION_COOKIE, token, httponly=True,
                            samesite="lax", path="/",
                            max_age=SESSION_TTL_DAYS * 24 * 3600,
                            secure=config.cookie_secure)
        return response

    @app.get("/account/logout")
    def logout(request: Request):
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
        jobs = store.list_jobs(account["id"])
        entries = store.list_credit_entries(account["id"], limit=10)
        # The website the visitor typed on the landing page, carried
        # through sign-in and checkout — it arrives prefilled on the
        # add-website form, never retyped.
        try:
            pending_url = validate_site_url(site_url) if site_url else ""
        except ValueError:
            pending_url = ""
        add_site_href = ("/account/sites/new?url=" +
                         urllib.parse.quote(pending_url, safe="") if
                         pending_url else "/account/sites/new")
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
        site_rows = ""
        for site in sites:
            connections = {c["kind"]: c
                           for c in store.get_connections(site["id"])}
            plausible_key = None
            raw = (store.get_connection(site["id"], "plausible") or {}
                   ).get("_detail_raw") or {}
            if connections.get("plausible", {}).get("status") == "connected":
                plausible_key = raw.get("api_key")
            funnel = _funnel_rows(store, site, plausible_key)
            site_rows += (
                f"<div class='card'><h3><a href='/account/sites/{esc(site['id'])}'>"
                f"{esc(site['url'])}</a></h3>"
                f"<div class='table-wrap'><table>{funnel}</table></div></div>")
        if not site_rows:
            site_rows = ("<div class='card'><p>No websites yet. "
                         f"<a href='{esc(add_site_href)}'>Add your first "
                         "website</a> — Ventoryn works to bring it its "
                         "first 100 paying customers.</p></div>")
        job_rows = "".join(
            f"<tr><td>{esc(j['job_type'].replace('_', ' '))}</td>"
            f"<td>{esc(j['status'])}</td>"
            f"<td>{j['credits_charged']} credits</td>"
            f"<td>{esc(j['result'] or '—')}</td></tr>"
            for j in jobs) or ("<tr><td colspan='4'>No jobs yet.</td></tr>")
        entry_rows = "".join(
            f"<tr><td>{'+' if e['delta'] > 0 else ''}{e['delta']}</td>"
            f"<td>{esc(e['reason'])}</td></tr>"
            for e in entries) or ("<tr><td colspan='2'>No credit "
                                  "activity yet.</td></tr>")
        inner = f"""{notice}
<div class='card'><h2>Your plan</h2>
<p>{status_badge} {esc(PLAN_NAME)} — {esc(PLAN_PRICE_LABEL)}</p>
<p>Credit balance: <span class='big'>{account['credits_balance']}</span>
credits &middot; {config.monthly_credits} credits included every month
while subscribed.</p>
<p><a class='btn btn-primary' href='/account/subscribe'>Subscribe — $20/month</a>
<a class='btn' href='{esc(add_site_href)}'>Add a website</a></p></div>
<h2>Your websites</h2>{site_rows}
<h2>Recent jobs</h2>
<div class='table-wrap'><table>
<tr><th>Job</th><th>Status</th><th>Cost</th><th>Result</th></tr>
{job_rows}</table></div>
<h2>Credit activity</h2>
<div class='table-wrap'><table>
<tr><th>Change</th><th>Reason</th></tr>{entry_rows}</table></div>
<h2>What a credit buys</h2>{_credit_table()}"""
        return _page("Your dashboard", inner, account)

    # -- subscribe -----------------------------------------------------------
    @app.get("/account/subscribe", response_class=HTMLResponse)
    def subscribe_page(request: Request, cancelled: str = "",
                       site_url: str = ""):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
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
            inner = (f"{notice}<div class='card'><p>You are subscribed "
                     f"to {esc(PLAN_NAME)} at {esc(PLAN_PRICE_LABEL)}."
                     "</p><p>Your monthly credits arrive with each "
                     "billing cycle.</p></div>")
            if pending_url:
                inner += (f"<div class='card'>{pending_note}<p><a class='btn "
                          f"btn-primary' href='/account/sites/new?url="
                          f"{esc(urllib.parse.quote(pending_url, safe=''))}'>"
                          f"Add {esc(pending_url)}</a></p></div>")
            return _page("Subscribe", inner, account)
        if not config.stripe_configured:
            inner = (f"{notice}<div class='card'><h2>{esc(PLAN_NAME)} — "
                     f"{esc(PLAN_PRICE_LABEL)}</h2><p>What it includes:</p>"
                     f"<ul><li>{config.monthly_credits} credits every "
                     f"month</li><li>Your websites on Ventoryn, working "
                     f"toward their first 100 paying customers</li>"
                     f"<li>Growth reports per site</li></ul>"
                     f"<p><strong>Payments are not set up yet.</strong> "
                     f"The price is {esc(PLAN_PRICE_LABEL)}; the Stripe "
                     f"connection is still being set up, so checkout is "
                     f"not available on this page yet.</p></div>"
                     f"<h2>What a credit buys</h2>{_credit_table()}")
            return _page("Subscribe", inner, account)
        inner = (f"{notice}<div class='card'><h2>{esc(PLAN_NAME)} — "
                 f"{esc(PLAN_PRICE_LABEL)}</h2><p>What it includes:</p>"
                 f"<ul><li>{config.monthly_credits} credits every month"
                 f"</li><li>Your websites on Ventoryn, working toward "
                 f"their first 100 paying customers</li>"
                 f"<li>Growth reports per site</li></ul>"
                 f"{pending_note}"
                 f"<form method='post' action='/account/subscribe'>"
                 f"{pending_field}"
                 f"<button class='btn btn-primary' type='submit'>"
                 f"Subscribe — $20/month</button></form>"
                 f"<p class='muted'>Checkout is handled by Stripe. "
                 f"Ventoryn never sees your card.</p></div>"
                 f"<h2>What a credit buys</h2>{_credit_table()}")
        return _page("Subscribe", inner, account)

    @app.post("/account/subscribe")
    def subscribe_start(request: Request, site_url: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        if account["subscription_status"] == "active":
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
                _page("Subscribe", f"<p>{esc(exc)}</p>"
                      "<p><a href='/account/subscribe'>Back</a>.</p>",
                      account),
                status_code=400)
        return RedirectResponse(session["url"], status_code=303)

    # -- sites ---------------------------------------------------------------
    @app.get("/account/sites/new", response_class=HTMLResponse)
    def site_new_page(request: Request):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        inner = ("<div class='card'><p>Add the website Ventoryn will "
                 "work on — it works to bring this site its first 100 "
                 "paying customers.</p>"
                 "<form method='post' action='/account/sites'>"
                 "<label for='url'>Website URL</label>"
                 "<input type='url' id='url' name='url' required "
                 "placeholder='https://example.com'>"
                 "<label for='country'>Market country</label>"
                 "<select id='country' name='country'>"
                 "<option value='ca' selected>Canada (ca)</option>"
                 "<option value='us'>United States (us)</option>"
                 "<option value='ht'>Haiti (ht)</option>"
                 "<option value='fr'>France (fr)</option>"
                 "<option value='gb'>United Kingdom (gb)</option>"
                 "</select>"
                 "<label for='language'>Market language</label>"
                 "<select id='language' name='language'>"
                 "<option value='en' selected>English (en)</option>"
                 "<option value='fr'>French (fr)</option>"
                 "<option value='ht'>Haitian Creole (ht)</option>"
                 "</select>"
                 "<p><button class='btn btn-primary' type='submit'>"
                 "Add website</button></p></form></div>")
        return _page("Add a website", inner, account)

    @app.post("/account/sites")
    def site_create(request: Request, url: str = Form(...),
                    country: str = Form("ca"), language: str = Form("en")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        try:
            site = store.create_site(account["id"], url, country, language)
        except ValueError as exc:
            return HTMLResponse(
                _page("Add a website",
                      f"<p>{esc(exc)}</p><p><a href='/account/sites/new'>"
                      "Back</a>.</p>", account),
                status_code=400)
        return RedirectResponse(f"/account/sites/{site['id']}",
                                status_code=303)

    @app.get("/account/sites/{site_id}", response_class=HTMLResponse)
    def site_detail(request: Request, site_id: str):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_page("Website", "<p>Website not found.</p>",
                                      account), status_code=404)
        connections = store.get_connections(site_id)
        plausible_raw = (store.get_connection(site_id, "plausible") or {}
                         ).get("_detail_raw") or {}
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
            if kind == "plausible":
                form = ("<form method='post' "
                        f"action='/account/sites/{esc(site_id)}/connections/plausible'>"
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
                        f"<form method='post' action='/account/sites/{esc(site_id)}"
                        f"/connections/stripe_sales'>"
                        "<label for='stripe_secret'>Stripe webhook signing "
                        "secret</label><input type='text' id='stripe_secret' "
                        "name='webhook_secret' placeholder='Stored, never "
                        "shown back'><p><button class='btn' type='submit'>"
                        "Connect Stripe sales</button></p></form>")
            else:
                form = ("<p class='muted'>Ventoryn cannot connect this "
                        "account yet — its own app registration is still "
                        "needed. Nothing is faked here.</p>")
            conn_rows += (f"<div class='card'><h3>{esc(kind.replace('_', ' '))} "
                          f"{badge}</h3><p>{esc(conn['note'])}</p>{form}</div>")
        jobs = store.list_jobs(account["id"], site_id=site_id)
        job_rows = "".join(
            f"<tr><td>{esc(j['job_type'].replace('_', ' '))}</td>"
            f"<td>{esc(j['status'])}</td>"
            f"<td>{j['credits_charged']} credits</td>"
            f"<td>{esc(j['result'] or '—')}</td></tr>"
            for j in jobs) or ("<tr><td colspan='4'>No jobs yet.</td></tr>")
        conversions = store.list_conversions(site_id)
        conv_rows = "".join(
            f"<tr><td>${c['amount_cents'] / 100:,.2f} "
            f"{esc(c['currency'].upper())}</td>"
            f"<td>{esc(c['source'])}</td>"
            f"<td>{esc(c['created_at'][:10])}</td></tr>"
            for c in conversions) or ("<tr><td colspan='3'>No paying "
                                      "customers recorded yet.</td></tr>")
        inner = f"""<div class='card'><h2>Funnel — visitors to paying customers</h2>
<div class='table-wrap'><table>{funnel}</table></div>
<p class='muted'>Every number here comes from a real source. Where a
connection is missing the value reads "unavailable" — never invented.</p></div>
<h2>Run agent work</h2>
<div class='card'><p>Credit balance: <strong>{account['credits_balance']}</strong></p>
<form method='post' action='/account/sites/{esc(site_id)}/jobs'>
<label for='job_type'>Job</label>
<select id='job_type' name='job_type'>
<option value='analyze'>Analyze my site — 5 credits (available now)</option>
<option value='seo_article' disabled>SEO article draft — 10 credits (coming soon)</option>
<option value='video_render' disabled>Video render — 15 credits (coming soon)</option>
<option value='community_listening' disabled>Community listening — 5 credits (coming soon)</option>
</select>
<p><button class='btn btn-primary' type='submit'>Run job</button></p></form>
<p class='muted'>Nothing is published or posted without your approval.</p></div>
<h2>Jobs for this site</h2>
<div class='table-wrap'><table>
<tr><th>Job</th><th>Status</th><th>Cost</th><th>Result</th></tr>
{job_rows}</table></div>
<h2>Paying customers</h2>
<div class='table-wrap'><table>
<tr><th>Amount</th><th>Source</th><th>Date</th></tr>
{conv_rows}</table></div>
<div class='card'><h3>Record a sale manually</h3>
<p class='muted'>If a customer paid outside Stripe, record it here so the
funnel counts it. This is the owner-recorded fallback.</p>
<form method='post' action='/account/sites/{esc(site_id)}/conversions'>
<label for='amount'>Amount (USD)</label>
<input type='text' id='amount' name='amount_usd' placeholder='19.00'>
<p><button class='btn' type='submit'>Record sale</button></p></form></div>
<h2>Connections</h2>{conn_rows}"""
        return _page(site["url"], inner, account)

    @app.post("/account/sites/{site_id}/connections/{kind}")
    def connection_update(request: Request, site_id: str, kind: str,
                          site_domain: str = Form(""),
                          api_key: str = Form(""),
                          webhook_secret: str = Form("")):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_page("Website", "<p>Website not found.</p>",
                                      account), status_code=404)
        if kind in ("reddit", "x"):
            return HTMLResponse(
                _page(site["url"],
                      "<p>This connection is not available yet — Ventoryn's "
                      "own app registration for it is still needed. No "
                      "account was connected and nothing was stored.</p>"
                      f"<p><a href='/account/sites/{esc(site_id)}'>Back</a>.</p>",
                      account),
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
                _page(site["url"], "<p>Unknown connection.</p>", account),
                status_code=400)
        return RedirectResponse(f"/account/sites/{site_id}", status_code=303)

    @app.post("/account/sites/{site_id}/jobs")
    def run_job(request: Request, site_id: str,
                job_type: str = Form(...)):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_page("Website", "<p>Website not found.</p>",
                                      account), status_code=404)
        cost = CREDIT_COSTS.get(job_type)
        if cost is None:
            return HTMLResponse(
                _page(site["url"], "<p>Unknown job type.</p>", account),
                status_code=400)
        if job_type not in RUNNABLE_JOB_TYPES:
            return HTMLResponse(
                _page(site["url"],
                      f"<p>{esc(job_type.replace('_', ' '))} is not "
                      f"available yet — it will cost {cost} credits when it "
                      f"lands. Nothing was charged.</p>"
                      f"<p><a href='/account/sites/{esc(site_id)}'>Back</a>.</p>",
                      account),
                status_code=400)
        balance = store.credit_balance(account["id"])
        if balance < cost:
            return HTMLResponse(
                _page(site["url"],
                      f"<p>That job costs {cost} credits and the balance is "
                      f"{balance}. Nothing ran and nothing was charged. "
                      f"Top up by subscribing on the "
                      f"<a href='/account/subscribe'>Subscribe</a> page.</p>",
                      account),
                status_code=402)
        # Charge first (atomic — the job row is created with the charge),
        # run second; a failed run is refunded, so nobody pays for work
        # that did not happen.
        try:
            job = store.charge_credits(account["id"], job_type, site_id)
        except InsufficientCredits as exc:
            return HTMLResponse(
                _page(site["url"],
                      f"<p>{esc(exc)}</p><p><a href='/account/sites/"
                      f"{esc(site_id)}'>Back</a>.</p>", account),
                status_code=402)
        try:
            from ecosystem.analyze import render_report, run_analysis

            result = run_analysis(site["url"], site["country"],
                                  site["language"])
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            report_name = (f"analyze_{slug_for_url(site['url'])}_{date}.md")
            (REPORTS_DIR / report_name).write_text(
                render_report(result), encoding="utf-8")
            summary = (f"Site {result['site_score']}/100 · "
                       f"GEO {result['geo_score']}/100 · "
                       f"{len(result['findings'])} findings · "
                       f"report {report_name}")
            store.set_job_result(job["id"], "done", summary)
        except Exception as exc:  # noqa: BLE001 - refund and say so
            store.grant_credits(account["id"], cost,
                                f"Refund — {job_type} job failed")
            store.set_job_result(job["id"], "failed",
                                 f"Failed — refunded {cost} credits ({exc})")
            return HTMLResponse(
                _page(site["url"],
                      f"<p>The job failed ({esc(exc)}) and the {cost} "
                      f"credits were refunded.</p>"
                      f"<p><a href='/account/sites/{esc(site_id)}'>Back</a>.</p>",
                      account),
                status_code=500)
        return RedirectResponse(f"/account/sites/{site_id}", status_code=303)

    @app.post("/account/sites/{site_id}/conversions")
    def record_sale(request: Request, site_id: str,
                    amount_usd: str = Form(...)):
        account, redirect = _need_account(request)
        if redirect is not None:
            return redirect
        site = store.get_site(site_id)
        if site is None or site["account_id"] != account["id"]:
            return HTMLResponse(_page("Website", "<p>Website not found.</p>",
                                      account), status_code=404)
        try:
            amount_cents = int(round(float(amount_usd) * 100))
            if amount_cents <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return HTMLResponse(
                _page(site["url"],
                      "<p>Enter a sale amount in dollars, e.g. 19.00.</p>"
                      f"<p><a href='/account/sites/{esc(site_id)}'>Back</a>.</p>",
                      account),
                status_code=400)
        store.record_conversion(site_id, amount_cents, "usd", "manual")
        return RedirectResponse(f"/account/sites/{site_id}", status_code=303)

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
            return JSONResponse({"received": True, "result": result})
        for site_id, secret in store.stripe_sales_secrets():
            if verify_webhook_signature(payload, header, secret):
                result = handle_customer_sale_webhook(store, site_id, event)
                return JSONResponse({"received": True, "result": result})
        return JSONResponse({"error": "invalid signature"}, status_code=400)
