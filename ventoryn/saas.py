"""Ventoryn SaaS shell — accounts, Stripe billing, credits, sites.

This is what turns the internal Ventoryn dashboard into a product a
website owner can buy: a stranger signs in with Google, subscribes for
$20/month, adds their website, and Ventoryn works to bring that site
its first 100 paying customers.

Design rules (from the brief, do not weaken):
- Sign-in is Google OAuth only. No passwords anywhere in this module.
- Stripe keys, Google client id/secret, and the price id all come from
  the environment. Nothing secret is ever written to code, tests, or logs.
- The price is $20/month (Spencer's decision). The Stripe price id
  itself is still env (STRIPE_PRICE_ID) — the $20 price is created in
  Stripe's dashboard, and its id is pasted into env.
- Credits are the only currency agent work spends. One documented
  cost table (CREDIT_COSTS); the monthly allowance is env-configurable
  (SAAS_MONTHLY_CREDITS, default 100 — a starting number Spencer can
  change). A job with insufficient credits is refused with the balance
  shown; balances never go negative.
- Every number on the customer dashboard comes from a real source
  (Plausible, the customer Stripe webhook, or the job log). Where a
  connection is missing the value reads "unavailable" — never invented,
  never zero-filled.
- NOTHING auto-publishes or posts. No agent refunds or charges.
  Approvals stay human.
- Stdlib only for HTTP (urllib) — no new dependencies. The HTTP layer
  is injectable (urlopen=...) so tests run hermetic with fakes.
- Storage is the shared SQLite state file (the same file businesses,
  the ledger and the audit log live in when ECOSYSTEM_STATE_DB is set).
  No new database. SaasStore(":memory:") is the hermetic default used
  by create_app() when no store is passed, so importing the dashboard
  never creates a file and tests never touch disk.
- TOKENS are stored ENCRYPTED at rest (ventoryn/token_crypto.py,
  VENTORYN_TOKEN_KEY), never plaintext, never sent to the browser.
  Disconnect revokes at the provider where the provider allows it.
- TWO-FACTOR is FORCED for every account from the first sign-in:
  a session whose account has not finished TOTP setup is limited to
  the setup page. The same TOTP module (ventoryn/totp.py) serves the
  admin login and the customer sign-in — one implementation.
- ATTRIBUTION: a sale is Ventoryn's only where the Ventoryn
  signature (ventoryn/signature.py — UTM + signed vtn_ tag) reaches the
  payment. Otherwise it is unattributed revenue, never guessed.
- REVENUE PRIVACY: the per-site "show my revenue" toggle is
  respected everywhere, including admin totals (off = no dollar
  figure anywhere; the sale still counts toward the 100 goal).
  Revenue figures never go into logs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from core.countries import normalize_country
from ventoryn.signature import make_tag, parse_tag, tag_from_url
from ventoryn.token_crypto import decrypt_token, encrypt_token, is_encrypted
from ventoryn.totp import (
    hash_backup_code,
    new_backup_codes,
    new_secret,
    verify_backup_code,
    verify_code,
)

# ---------------------------------------------------------------------------
# Configuration (environment only)
# ---------------------------------------------------------------------------

PLAN_NAME = "Ventoryn Growth"
PLAN_PRICE_USD = 20  # $20/month — Spencer's decision (2026-11-08).
PLAN_PRICE_LABEL = "$20/month"

#: Starting monthly credit allowance for the $20 plan. Configurable via
#: SAAS_MONTHLY_CREDITS; this default is a starting number for Spencer
#: to change, not a researched price point.
MONTHLY_CREDIT_ALLOWANCE_DEFAULT = 100

#: What one run of each kind of agent work costs, in credits. This is
#: the starting table Spencer can change. Only "analyze" is runnable
#: through the shell today (see ventoryn/saas_routes.py); the rest are
#: priced here so the plan page and the cost table stay honest about
#: what each job will cost when it lands.
CREDIT_COSTS: dict[str, int] = {
    "analyze": 5,              # site + GEO audit of one URL
    "geo_audit": 5,            # GEO answer-readiness audit
    "community_listening": 5,  # one listening run (Reddit, past week)
    "social_report": 5,        # social analytics report from an export
    "seo_article": 10,         # one SEO blog article draft
    "video_render": 15,        # one rendered video (script -> MP4 + SRT)
}

#: Job types the shell can actually run right now. The rest of
#: CREDIT_COSTS is priced but not yet wired to the agent machinery.
RUNNABLE_JOB_TYPES = frozenset({"analyze"})

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_SCOPES = "openid email profile"

STRIPE_API_BASE = "https://api.stripe.com/v1"
STRIPE_CHECKOUT_SESSIONS_URL = f"{STRIPE_API_BASE}/checkout/sessions"

SESSION_COOKIE = "ventoryn_session"
SESSION_TTL_DAYS = 30
OAUTH_STATE_TTL_MINUTES = 10
WEBHOOK_TIMESTAMP_TOLERANCE_S = 300


class SaasError(Exception):
    """A plain-language shell failure. Never carries a secret."""


class InsufficientCredits(SaasError):
    """A job was refused because the balance cannot cover its cost."""

    def __init__(self, cost: int, balance: int) -> None:
        super().__init__(
            f"That job costs {cost} credits and the balance is {balance}.")
        self.cost = cost
        self.balance = balance


@dataclass
class SaasConfig:
    """Everything the shell reads from the environment. Empty values
    mean "not configured yet" — pages say so plainly instead of
    failing or faking it."""

    google_client_id: str = ""
    google_client_secret: str = ""
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""
    monthly_credits: int = MONTHLY_CREDIT_ALLOWANCE_DEFAULT
    base_url: str = "http://localhost:8000"
    cookie_secure: bool = False
    #: The operator's Google email (Spencer). The operator signs in
    #: with Google like anyone else; this email is what gates the
    #: operator tools and ownership of the existing ecosystem
    #: businesses. Read from the environment (SAAS_OPERATOR_EMAIL) —
    #: never a hardcoded secret, just an email address. Empty means
    #: "no operator configured". Note: this email does NOT open
    #: /admin by itself — the admin needs its own password (below).
    operator_email: str = ""
    #: The admin password (Spencer's). /admin requires this password
    #: and nothing else — a signed-in Google session alone never
    #: opens it. Read from the environment (VENTORYN_ADMIN_PASSWORD).
    #: Empty means "no admin password configured" and login always
    #: fails (fail closed). The value is never logged or rendered.
    admin_password: str = ""
    #: Whether the operator has registered the Reddit / X OAuth apps
    #: in the developer consoles (VENTORYN_REDDIT_APP_REGISTERED /
    #: VENTORYN_X_APP_REGISTERED = "1"). Until an app is registered,
    #: that connection honestly reports "not connected — app
    #: registration needed" and no posting is possible on it.
    reddit_app_registered: bool = False
    x_app_registered: bool = False

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SaasConfig":
        env = os.environ if env is None else env
        try:
            monthly = int(env.get("SAAS_MONTHLY_CREDITS",
                                  MONTHLY_CREDIT_ALLOWANCE_DEFAULT))
        except ValueError:
            monthly = MONTHLY_CREDIT_ALLOWANCE_DEFAULT
        return cls(
            google_client_id=env.get("GOOGLE_CLIENT_ID", ""),
            google_client_secret=env.get("GOOGLE_CLIENT_SECRET", ""),
            stripe_secret_key=env.get("STRIPE_SECRET_KEY", ""),
            stripe_webhook_secret=env.get("STRIPE_WEBHOOK_SECRET", ""),
            stripe_price_id=env.get("STRIPE_PRICE_ID", ""),
            monthly_credits=monthly,
            base_url=env.get("SAAS_BASE_URL", "http://localhost:8000"
                             ).rstrip("/"),
            cookie_secure=env.get("SAAS_COOKIE_SECURE", "") == "1",
            operator_email=env.get("SAAS_OPERATOR_EMAIL", ""
                                   ).strip().lower(),
            admin_password=env.get("VENTORYN_ADMIN_PASSWORD", ""),
            reddit_app_registered=env.get(
                "VENTORYN_REDDIT_APP_REGISTERED", "") == "1",
            x_app_registered=env.get("VENTORYN_X_APP_REGISTERED", "") == "1",
        )

    def app_registered(self, kind: str) -> bool:
        """True when the operator has registered the OAuth app for a
        posting provider. Plausible and Stripe need no app
        registration (API key / webhook secret), so they are always
        "registered"."""
        if kind == "reddit":
            return self.reddit_app_registered
        if kind == "x":
            return self.x_app_registered
        return True

    @property
    def admin_configured(self) -> bool:
        """True only when an admin password is set. Unset means the
        admin login always fails — fail closed, never open."""
        return bool(self.admin_password)

    def is_operator(self, email: str | None) -> bool:
        """True when this Google email is the operator's. Case- and
        whitespace-insensitive; False when no operator is configured."""
        return bool(self.operator_email and email and
                    email.strip().lower() == self.operator_email)

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def stripe_configured(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_price_id)

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.base_url}/account/auth/google/callback"


def default_saas_db_path() -> Path | None:
    """The shared state file when ECOSYSTEM_STATE_DB is set, else None
    (the caller falls back to an in-memory store — same rule as the
    businesses, ledger and audit log, so a dashboard without a
    configured state file never writes one into the tree)."""
    shared = os.environ.get("ECOSYSTEM_STATE_DB")
    return Path(shared) if shared else None


# ---------------------------------------------------------------------------
# Site URL validation — same rule as analyze (http/https only), plus the
# localhost / private-address rejection the shell needs for customer
# sites. A bare domain works: https:// is added when no scheme is
# present. A URL that fails here is never fetched and never stored.
# ---------------------------------------------------------------------------

def _normalize_scheme(raw: str) -> str:
    """A bare domain works: prepend https:// when no scheme is
    present, after trimming whitespace."""
    url = raw.strip()
    if "://" not in url:
        url = "https://" + url
    return url


def validate_site_url(value: str) -> str:
    """Normalize a customer website URL or raise ValueError with a
    plain-language reason. Rejects empty input, non-URLs, non-http(s)
    schemes, and localhost / private / link-local hosts."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Enter a website URL, e.g. example.com")
    raw = _normalize_scheme(raw)
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            "That is not a website URL — try a domain like example.com")
    host = (parsed.hostname or "").lower()
    if host in ("localhost", ""):
        raise ValueError("Local addresses are not websites — use a public URL")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None:
        # A domain name: letters, digits, hyphens and dots, with at
        # least one dot — so "not a url" is rejected instead of
        # being stored as a site.
        if ("." not in host
                or not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?",
                                    host)
                or ".." in host):
            raise ValueError(
                "That is not a website URL — try a domain like example.com")
    if ip is not None and (ip.is_private or ip.is_loopback
                           or ip.is_link_local or ip.is_reserved
                           or ip.is_multicast):
        raise ValueError("That address is not a public website")
    if host.endswith((".localhost", ".local", ".internal", ".lan")):
        raise ValueError("That address is not a public website")
    path = parsed.path or ""
    return f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/") or \
        f"{parsed.scheme}://{parsed.netloc}"


def slug_for_url(url: str) -> str:
    """Filesystem-safe slug for a site URL (matches the analyze report
    naming: reports/analyze_<host>_<date>.md)."""
    host = urlparse(url).netloc.lower()
    return "".join(c if c.isalnum() else "-" for c in host).strip("-") or "site"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _new_id() -> str:
    return secrets.token_hex(12)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
# Store — SQLite tables in the shared state file (same pattern as
# core/state_store.py: one connection, a lock, WAL, CREATE TABLE IF NOT
# EXISTS). Secrets are stored only where the feature needs them
# (a site's Plausible API key / Stripe webhook secret, needed to call
# those services on the customer's behalf) and are never returned by
# any read method, never logged, and never rendered into a page.
# ---------------------------------------------------------------------------

_SAAS_SCHEMA = """
CREATE TABLE IF NOT EXISTS saas_accounts (
    id TEXT PRIMARY KEY,
    google_sub TEXT UNIQUE NOT NULL,
    email TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    subscription_status TEXT NOT NULL DEFAULT 'none',
    current_period_end TEXT,
    credits_balance INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS saas_sessions (
    token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_oauth_states (
    state TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_sites (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    url TEXT NOT NULL,
    host TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT 'ca',
    language TEXT NOT NULL DEFAULT 'en',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_connections (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'not_connected',
    note TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    UNIQUE(site_id, kind)
);
CREATE TABLE IF NOT EXISTS saas_jobs (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    job_type TEXT NOT NULL,
    credits_charged INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_credit_ledger (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_conversions (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'usd',
    source TEXT NOT NULL,
    stripe_event_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_stripe_events (
    event_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_admin_sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_activity (
    account_id TEXT PRIMARY KEY,
    last_seen_at TEXT NOT NULL,
    last_action TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS saas_activity_events (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_signatures (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    action_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    job_id TEXT NOT NULL DEFAULT '',
    target_url TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_admin_attempts (
    key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS saas_admin_totp (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    secret_encrypted TEXT NOT NULL DEFAULT '',
    salt TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0,
    backup_hashes TEXT NOT NULL DEFAULT '[]',
    pending_secret TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

#: Columns added to tables that already exist on a deployed state
#: file. __init__ adds each one that is missing (CREATE TABLE IF NOT
#: EXISTS cannot add a column to an existing table). Never remove an
#: entry from this list.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("saas_accounts", "totp_secret", "TEXT NOT NULL DEFAULT ''"),
    ("saas_accounts", "totp_salt", "TEXT NOT NULL DEFAULT ''"),
    ("saas_accounts", "totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("saas_accounts", "totp_backup_hashes", "TEXT NOT NULL DEFAULT '[]'"),
    ("saas_accounts", "totp_pending_secret", "TEXT NOT NULL DEFAULT ''"),
    ("saas_accounts", "posting_paused", "INTEGER NOT NULL DEFAULT 0"),
    # Revenue privacy is THREE SEPARATE settings (consolidated brief
    # §7 + the attribution reference): revenue_show = dollar amounts;
    # count_show = the paying-customer count; details_show = the
    # per-sale detail rows (which campaign, when, attribution state).
    # Revenue is private by default (OFF); the count and the details
    # are shown by default (ON). Either way the sale is still
    # recorded and still counts toward the 100 goal internally.
    ("saas_sites", "revenue_show", "INTEGER NOT NULL DEFAULT 0"),
    ("saas_sites", "count_show", "INTEGER NOT NULL DEFAULT 1"),
    ("saas_sites", "details_show", "INTEGER NOT NULL DEFAULT 1"),
    ("saas_sessions", "totp_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("saas_admin_sessions", "totp_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("saas_conversions", "attribution", "TEXT NOT NULL DEFAULT 'unattributed'"),
    ("saas_conversions", "signature_tag", "TEXT NOT NULL DEFAULT ''"),
    ("saas_conversions", "action_id", "TEXT NOT NULL DEFAULT ''"),
)

#: Admin login rate limit: this many wrong guesses lock the login
#: for this long. The lockout key is the client address (or "unknown"
#: when the app cannot see one).
ADMIN_LOGIN_MAX_FAILURES = 5
ADMIN_LOGIN_LOCKOUT_MINUTES = 15

#: The admin session cookie (password login for /admin — separate
#: from the Google sign-in session). 24 hours, sliding: every valid
#: check extends it.
ADMIN_SESSION_COOKIE = "ventoryn_admin"
ADMIN_SESSION_TTL_HOURS = 24

#: "Live now" window on /admin: an account counts as live when it
#: was last seen within this many minutes. Stated on the page.
LIVE_WINDOW_MINUTES = 5

#: Connection kinds every site gets at onboarding. The note is what the
#: customer sees before they connect anything — it must never claim a
#: connection that does not exist.
DEFAULT_CONNECTIONS = (
    ("reddit", "not_connected",
     "Not connected — a Reddit app registration is needed before "
     "Ventoryn can post or reply as you."),
    ("x", "not_connected",
     "Not connected — an X app registration is needed before "
     "Ventoryn can post or reply as you."),
    ("plausible", "not_connected",
     "Not connected — add your Plausible site domain and API key to "
     "see real visitors here."),
    ("stripe_sales", "not_connected",
     "Not connected — point your Stripe webhook at the URL below to "
     "see real paying customers here."),
)

#: Secret fields inside a connection's detail JSON. Never returned by
#: get_connections(), never logged, never rendered.
_SECRET_DETAIL_KEYS = frozenset({"api_key", "webhook_secret"})

#: Minimum OAuth / API scopes requested for each connection. The
#: Connect screen shows these in plain words (see
#: CONNECTION_CONSENT) and the tests assert the strings.
CONNECTION_SCOPES: dict[str, str] = {
    # Reddit OAuth: post + reply, read the identity the post goes
    # out under. Never DMs, never account settings.
    "reddit": "submit read identity",
    # X OAuth 2.0: post + read. Never DMs (X has no DM scope in the
    # free tier and Ventoryn never asks for it).
    "x": "tweet.read tweet.write users.read offline.access",
    # Plausible API key: read-only stats for the site.
    "plausible": "stats:read",
    # Stripe sales: the customer's own webhook — read payments only.
    # Ventoryn never charges or refunds through it.
    "stripe_sales": "read payments",
}

#: Plain-words consent text shown on each Connect screen: exactly
#: what Ventoryn can do as the customer, and what it cannot do.
CONNECTION_CONSENT: dict[str, str] = {
    "reddit": ("Ventoryn can post and reply on Reddit as you, and read "
               "your Reddit identity so replies go out under your name. "
               "It cannot send direct messages, change your account "
               "settings, or do anything else on Reddit. Nothing is "
               "published without your approval first."),
    "x": ("Ventoryn can post and reply on X as you. It cannot send "
          "direct messages, change your account settings, or do "
          "anything else on X. Nothing is published without your "
          "approval first. X's API is paid and rate-limited, so "
          "posting on X is slower and may cost extra."),
    "plausible": ("Ventoryn can read visitor counts for your site from "
                  "Plausible. It cannot change your Plausible account "
                  "or see any other site on it."),
    "stripe_sales": ("Ventoryn can read payments on your Stripe account "
                     "so it can count paying customers. It cannot charge "
                     "a card, refund a payment, or change anything in "
                     "your Stripe account."),
}

#: Kinds whose connection needs the operator to register an OAuth
#: app in the provider's developer console first (Spencer's step).
#: Until then the connection honestly says so and cannot be used.
APP_REGISTRATION_KINDS = frozenset({"reddit", "x"})

#: What happens to the provider token on Disconnect, per kind.
#: reddit/x are revoked by an HTTP call to the provider (see
#: revoke_provider_token); plausible keys and Stripe webhook secrets
#: have no provider revoke API, so the token is deleted here and the
#: customer is told to remove it in the provider's dashboard.
CONNECTION_REVOKE_NOTE: dict[str, str] = {
    "reddit": "The Reddit token is revoked at Reddit when you disconnect.",
    "x": "The X token is revoked at X when you disconnect.",
    "plausible": ("Plausible has no API to delete a key — remove this "
                  "key in your Plausible dashboard after disconnecting."),
    "stripe_sales": ("Remove the webhook endpoint in your Stripe "
                     "dashboard after disconnecting."),
}


class SaasStore:
    """All SaaS state. db_path is a file path, or ":memory:" for the
    hermetic default (tests and plain create_app()).

    `token_key` is the encryption key for stored OAuth/API tokens
    (VENTORYN_TOKEN_KEY). When it is None the environment is read.
    When no key is configured at all, a FILE store refuses to store
    a token (fail closed — a token is never written in plaintext);
    an in-memory store uses an ephemeral key, which is safe because
    nothing in it survives the process."""

    def __init__(self, db_path: str | Path = ":memory:",
                 token_key: str | None = None) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        env_key = os.environ.get("VENTORYN_TOKEN_KEY", "")
        self._token_key = token_key if token_key is not None else (
            env_key or None)
        if self._token_key is None and self.db_path == ":memory:":
            self._token_key = secrets.token_hex(32)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                     timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SAAS_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns that newer code needs to tables that already
        exist on a deployed state file."""
        for table, column, ddl in _MIGRATIONS:
            have = {r[1] for r in self._conn.execute(
                f"PRAGMA table_info({table})")}
            if column not in have:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _encrypt_secret(self, value: str) -> str:
        """Encrypt one token value. Raises SaasError (fail closed)
        when no encryption key is configured for a file store."""
        if self._token_key is None:
            raise SaasError(
                "Token encryption is not configured — the operator must "
                "set VENTORYN_TOKEN_KEY before a token can be stored. "
                "Nothing was saved.")
        return encrypt_token(value, key=self._token_key)

    def _decrypt_secret(self, stored: str) -> str:
        if not stored:
            return ""
        if is_encrypted(stored):
            if self._token_key is None:
                raise SaasError(
                    "Token encryption is not configured — the stored "
                    "token cannot be read.")
            return decrypt_token(stored, key=self._token_key)
        # A legacy plaintext value (written before encryption landed):
        # readable, but never written again.
        return stored

    def _encrypt_detail(self, detail: dict) -> dict:
        """Encrypt the secret fields of a connection detail dict."""
        out = dict(detail)
        for key in _SECRET_DETAIL_KEYS:
            if out.get(key):
                out[key] = self._encrypt_secret(str(out[key]))
        return out

    def _decrypt_detail(self, detail: dict) -> dict:
        out = dict(detail)
        for key in _SECRET_DETAIL_KEYS:
            if out.get(key):
                out[key] = self._decrypt_secret(str(out[key]))
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- accounts ------------------------------------------------------------
    def create_account(self, google_sub: str, email: str,
                       name: str = "") -> dict:
        now = _iso(_now())
        account_id = _new_id()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO saas_accounts (id, google_sub, email, name,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (account_id, google_sub, email, name, now))
        return self.get_account(account_id)

    def get_account(self, account_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_accounts WHERE id = ?",
                (account_id,)).fetchone()
        return _row_dict(row) if row else None

    def get_account_by_sub(self, google_sub: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_accounts WHERE google_sub = ?",
                (google_sub,)).fetchone()
        return _row_dict(row) if row else None

    def get_account_by_stripe_customer(self, customer_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_accounts WHERE stripe_customer_id = ?",
                (customer_id,)).fetchone()
        return _row_dict(row) if row else None

    def get_account_by_subscription(self, subscription_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_accounts WHERE stripe_subscription_id = ?",
                (subscription_id,)).fetchone()
        return _row_dict(row) if row else None

    def list_accounts(self) -> list[dict]:
        """Every account on the platform, oldest first. Operator-only
        data — the /admin pages are the only caller; a customer's own
        pages never list other accounts."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saas_accounts ORDER BY created_at"
                ).fetchall()
        return [_row_dict(r) for r in rows]

    def set_stripe_customer(self, account_id: str, customer_id: str,
                            subscription_id: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_accounts SET stripe_customer_id = ?,"
                " stripe_subscription_id = COALESCE(?, stripe_subscription_id)"
                " WHERE id = ?",
                (customer_id, subscription_id, account_id))

    def set_subscription(self, account_id: str, status: str,
                         subscription_id: str | None = None,
                         current_period_end: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_accounts SET subscription_status = ?,"
                " stripe_subscription_id = COALESCE(?, stripe_subscription_id),"
                " current_period_end = COALESCE(?, current_period_end)"
                " WHERE id = ?",
                (status, subscription_id, current_period_end, account_id))

    # -- sessions ------------------------------------------------------------
    def create_session(self, account_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO saas_sessions (token_hash, account_id,"
                " created_at, expires_at) VALUES (?, ?, ?, ?)",
                (_hash_token(token), account_id, _iso(now),
                 _iso(now + timedelta(days=SESSION_TTL_DAYS))))
        return token

    def get_account_by_session(self, token: str) -> dict | None:
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT a.* FROM saas_accounts a JOIN saas_sessions s"
                " ON s.account_id = a.id WHERE s.token_hash = ?"
                " AND s.expires_at > ?",
                (_hash_token(token), _iso(_now()))).fetchone()
        return _row_dict(row) if row else None

    def delete_session(self, token: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM saas_sessions WHERE token_hash = ?",
                               (_hash_token(token),))

    def get_session(self, token: str) -> dict | None:
        """The session row for a token (account id, 2FA-verified
        flag, expiry) — the zero-trust check: every request validates
        the session server-side, expiry enforced, a deleted session
        is rejected on the next request."""
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_sessions WHERE token_hash = ?",
                (_hash_token(token),)).fetchone()
        return _row_dict(row) if row else None

    def mark_session_totp_verified(self, token: str) -> None:
        """Mark a session as having passed 2FA (code or backup code).
        A session that has not passed is limited to the 2FA setup /
        verify page — it opens no product page."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_sessions SET totp_verified = 1"
                " WHERE token_hash = ?", (_hash_token(token),))

    # -- two-factor: customer accounts -------------------------------------
    # One TOTP implementation (ventoryn/totp.py) shared with the admin
    # login. The secret is stored ENCRYPTED; backup codes are stored
    # only as salted hashes. Setup is a two-step flow: the secret is
    # generated into totp_pending_secret, and only moves to
    # totp_secret (enabled) once the customer proves it by entering
    # a correct code.
    def begin_totp_setup(self, account_id: str) -> dict:
        """Start (or restart) TOTP setup: a fresh secret + fresh
        backup codes. Returns the PLAINTEXT secret and codes — the
        caller shows them ONCE on the setup page and never stores
        them in plaintext."""
        secret = new_secret()
        salt = secrets.token_hex(8)
        codes = new_backup_codes()
        hashes = [hash_backup_code(c, salt) for c in codes]
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_accounts SET totp_pending_secret = ?,"
                " totp_salt = ?, totp_backup_hashes = ? WHERE id = ?",
                (self._encrypt_secret(secret), salt, json.dumps(hashes),
                 account_id))
        return {"secret": secret, "backup_codes": codes}

    def totp_state(self, account_id: str) -> dict:
        """{"enabled": bool, "has_pending": bool} — whether the
        account has finished 2FA setup, and whether a setup is in
        progress."""
        account = self.get_account(account_id)
        if not account:
            return {"enabled": False, "has_pending": False}
        return {"enabled": bool(account.get("totp_enabled")),
                "has_pending": bool(account.get("totp_pending_secret"))}

    def get_totp_pending_secret(self, account_id: str) -> str:
        """The PLAINTEXT pending secret for a setup in progress (so
        the setup page can re-show its QR code if the customer
        navigates away mid-setup). Empty when no setup is pending."""
        account = self.get_account(account_id)
        if not account or not account.get("totp_pending_secret"):
            return ""
        try:
            return self._decrypt_secret(account["totp_pending_secret"])
        except SaasError:
            return ""

    def confirm_totp_setup(self, account_id: str, code: str) -> bool:
        """Complete setup: the code must match the pending secret.
        On success the secret becomes active, 2FA is enabled, and
        the pending secret is cleared. Returns False (setup stays
        pending) on a wrong code."""
        account = self.get_account(account_id)
        if not account or not account.get("totp_pending_secret"):
            return False
        try:
            secret = self._decrypt_secret(account["totp_pending_secret"])
        except SaasError:
            return False
        if not verify_code(secret, code):
            return False
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_accounts SET totp_secret = ?,"
                " totp_pending_secret = '', totp_enabled = 1 WHERE id = ?",
                (account["totp_pending_secret"], account_id))
        return True

    def verify_totp_code(self, account_id: str, code: str) -> bool:
        """True for a valid current TOTP code for the account's
        ACTIVE secret. Backup codes are NOT accepted here — use
        consume_backup_code for those."""
        account = self.get_account(account_id)
        if not account or not account.get("totp_enabled"):
            return False
        if not account.get("totp_secret"):
            return False
        try:
            secret = self._decrypt_secret(account["totp_secret"])
        except SaasError:
            return False
        return verify_code(secret, code)

    def consume_backup_code(self, account_id: str, code: str) -> bool:
        """True (and the code is burned — it can never work again)
        when `code` is one of the account's unused backup codes."""
        account = self.get_account(account_id)
        if not account or not account.get("totp_backup_hashes"):
            return False
        try:
            stored = json.loads(account["totp_backup_hashes"])
        except (ValueError, TypeError):
            return False
        hit = verify_backup_code(code, account.get("totp_salt") or "", stored)
        if hit is None:
            return False
        stored = [h for h in stored if h != hit]
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_accounts SET totp_backup_hashes = ? WHERE id = ?",
                (json.dumps(stored), account_id))
        return True

    def regenerate_backup_codes(self, account_id: str) -> list[str]:
        """Generate a FRESH set of backup codes (replacing any
        existing ones) and return the plaintext codes — the caller
        shows them ONCE (on the setup-success page) and never stores
        them in plaintext. Used right after a successful setup, so
        the codes the customer saves are the ones that work."""
        codes = new_backup_codes()
        salt = secrets.token_hex(8)
        hashes = [hash_backup_code(c, salt) for c in codes]
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_accounts SET totp_salt = ?, "
                "totp_backup_hashes = ? WHERE id = ?",
                (salt, json.dumps(hashes), account_id))
        return codes

    def reset_totp(self, account_id: str) -> None:
        """Operator reset: wipes the secret, the backup codes and the
        enabled flag. The account must set up 2FA again at its next
        sign-in. The caller records this as an audited admin action."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_accounts SET totp_secret = '',"
                " totp_pending_secret = '', totp_enabled = 0,"
                " totp_backup_hashes = '[]' WHERE id = ?",
                (account_id,))

    def set_posting_paused(self, account_id: str, paused: bool) -> None:
        """The per-account "pause all posting" switch. Paused means
        no agent may post or reply as this account on any site."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_accounts SET posting_paused = ? WHERE id = ?",
                (1 if paused else 0, account_id))

    def delete_account(self, account_id: str) -> None:
        """Delete an account and everything it owns: sites and their
        connections (tokens included — the caller revokes at the
        provider FIRST), jobs, credit ledger, conversions, sessions,
        activity. The account row itself is deleted last."""
        with self._lock, self._conn:
            site_ids = [r[0] for r in self._conn.execute(
                "SELECT id FROM saas_sites WHERE account_id = ?",
                (account_id,)).fetchall()]
            for site_id in site_ids:
                self._conn.execute(
                    "DELETE FROM saas_connections WHERE site_id = ?",
                    (site_id,))
                self._conn.execute(
                    "DELETE FROM saas_conversions WHERE site_id = ?",
                    (site_id,))
                self._conn.execute(
                    "DELETE FROM saas_signatures WHERE site_id = ?",
                    (site_id,))
                self._conn.execute(
                    "DELETE FROM saas_sites WHERE id = ?", (site_id,))
            self._conn.execute(
                "DELETE FROM saas_jobs WHERE account_id = ?", (account_id,))
            self._conn.execute(
                "DELETE FROM saas_credit_ledger WHERE account_id = ?",
                (account_id,))
            self._conn.execute(
                "DELETE FROM saas_sessions WHERE account_id = ?",
                (account_id,))
            self._conn.execute(
                "DELETE FROM saas_activity WHERE account_id = ?",
                (account_id,))
            self._conn.execute(
                "DELETE FROM saas_activity_events WHERE account_id = ?",
                (account_id,))
            self._conn.execute(
                "DELETE FROM saas_accounts WHERE id = ?", (account_id,))

    # -- OAuth states (CSRF protection on the Google callback) ---------------
    def create_oauth_state(self) -> str:
        state = secrets.token_urlsafe(24)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO saas_oauth_states (state, created_at)"
                " VALUES (?, ?)", (state, _iso(_now())))
        return state

    def consume_oauth_state(self, state: str) -> bool:
        """True only for a state this shell issued within the last
        10 minutes. Consumed states are deleted — no replays."""
        if not state:
            return False
        cutoff = _iso(_now() - timedelta(minutes=OAUTH_STATE_TTL_MINUTES))
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT state FROM saas_oauth_states WHERE state = ?"
                " AND created_at > ?", (state, cutoff)).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "DELETE FROM saas_oauth_states WHERE state = ?", (state,))
        return True

    # -- credits -------------------------------------------------------------
    def credit_balance(self, account_id: str) -> int:
        account = self.get_account(account_id)
        return int(account["credits_balance"]) if account else 0

    def grant_credits(self, account_id: str, amount: int, reason: str) -> int:
        """Add credits (monthly allowance, top-up). Returns the new balance."""
        now = _iso(_now())
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_accounts SET credits_balance = credits_balance + ?"
                " WHERE id = ?", (amount, account_id))
            self._conn.execute(
                "INSERT INTO saas_credit_ledger (id, account_id, delta, reason,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (_new_id(), account_id, amount, reason, now))
        return self.credit_balance(account_id)

    def charge_credits(self, account_id: str, job_type: str,
                       site_id: str) -> dict:
        """Charge one job's credits and record the job, atomically.
        Raises InsufficientCredits (balance shown, nothing charged)
        or SaasError for an unknown job type. Balances never go negative."""
        cost = CREDIT_COSTS.get(job_type)
        if cost is None:
            raise SaasError(f"Unknown job type: {job_type}")
        now = _iso(_now())
        job_id = _new_id()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT credits_balance FROM saas_accounts WHERE id = ?",
                    (account_id,)).fetchone()
                balance = int(row[0]) if row else 0
                if balance < cost:
                    raise InsufficientCredits(cost, balance)
                self._conn.execute(
                    "UPDATE saas_accounts SET credits_balance ="
                    " credits_balance - ? WHERE id = ?",
                    (cost, account_id))
                self._conn.execute(
                    "INSERT INTO saas_credit_ledger (id, account_id, delta,"
                    " reason, created_at) VALUES (?, ?, ?, ?, ?)",
                    (_new_id(), account_id, -cost,
                     f"{job_type} job", now))
                self._conn.execute(
                    "INSERT INTO saas_jobs (id, account_id, site_id, job_type,"
                    " credits_charged, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                    (job_id, account_id, site_id, job_type, cost, now))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_dict(row) if row else None

    def set_job_result(self, job_id: str, status: str, result: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_jobs SET status = ?, result = ? WHERE id = ?",
                (status, result, job_id))

    def list_jobs(self, account_id: str, site_id: str | None = None,
                  limit: int = 20) -> list[dict]:
        with self._lock:
            if site_id:
                rows = self._conn.execute(
                    "SELECT * FROM saas_jobs WHERE account_id = ?"
                    " AND site_id = ? ORDER BY created_at DESC LIMIT ?",
                    (account_id, site_id, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM saas_jobs WHERE account_id = ?"
                    " ORDER BY created_at DESC LIMIT ?",
                    (account_id, limit)).fetchall()
        return [_row_dict(r) for r in rows]

    def list_credit_entries(self, account_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saas_credit_ledger WHERE account_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (account_id, limit)).fetchall()
        return [_row_dict(r) for r in rows]

    # -- sites + connections ---------------------------------------------------
    def create_site(self, account_id: str, url: str, country: str = "ca",
                    language: str = "en") -> dict:
        clean = validate_site_url(url)
        # Any real ISO 3166-1 code is accepted; anything else raises.
        clean_country = normalize_country(country)
        site_id = _new_id()
        now = _iso(_now())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO saas_sites (id, account_id, url, host, country,"
                " language, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (site_id, account_id, clean, urlparse(clean).netloc,
                 clean_country,
                 (language or "en").lower()[:2], now))
            for kind, status, note in DEFAULT_CONNECTIONS:
                self._conn.execute(
                    "INSERT INTO saas_connections (id, site_id, kind, status,"
                    " note, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (_new_id(), site_id, kind, status, note, now))
        return self.get_site(site_id)

    def set_revenue_show(self, site_id: str, show: bool) -> None:
        """The customer's revenue-AMOUNT display choice ("Show my
        revenue on Ventoryn"). OFF hides every dollar figure for the
        site — report, funnel and admin totals — while the
        paying-customer count still increments toward the 100 goal.
        The count display is a separate setting (set_count_show)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_sites SET revenue_show = ? WHERE id = ?",
                (1 if show else 0, site_id))

    def set_count_show(self, site_id: str, show: bool) -> None:
        """The customer's paying-customer COUNT display choice
        ("Show my customer count on Ventoryn"). OFF hides the count
        from the report, the funnel and the admin totals — the sales
        are still recorded and still count toward the 100 goal
        internally. Separate from the revenue-amount setting."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_sites SET count_show = ? WHERE id = ?",
                (1 if show else 0, site_id))

    def set_details_show(self, site_id: str, show: bool) -> None:
        """The customer's conversion-DETAILS display choice ("Show
        conversion details on Ventoryn"). OFF hides the per-sale
        rows (which campaign, when, attribution state) from the site
        page and the report. Separate from the revenue-amount and
        count settings."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_sites SET details_show = ? WHERE id = ?",
                (1 if show else 0, site_id))

    def get_site(self, site_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_sites WHERE id = ?", (site_id,)).fetchone()
        return _row_dict(row) if row else None

    def list_sites(self, account_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saas_sites WHERE account_id = ?"
                " ORDER BY created_at",
                (account_id,)).fetchall()
        return [_row_dict(r) for r in rows]

    def get_connections(self, site_id: str) -> list[dict]:
        """Connection records with secrets stripped — safe to render."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saas_connections WHERE site_id = ?"
                " ORDER BY kind",
                (site_id,)).fetchall()
        out = []
        for r in rows:
            d = _row_dict(r)
            detail = json.loads(d.get("detail") or "{}")
            d["detail"] = {k: v for k, v in detail.items()
                           if k not in _SECRET_DETAIL_KEYS}
            out.append(d)
        return out

    def get_connection(self, site_id: str, kind: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_connections WHERE site_id = ? AND kind = ?",
                (site_id, kind)).fetchone()
        if not row:
            return None
        d = _row_dict(row)
        d["_detail_raw"] = self._decrypt_detail(
            json.loads(d.get("detail") or "{}"))
        return d

    def update_connection(self, site_id: str, kind: str, status: str,
                          note: str, detail: dict | None = None) -> None:
        """Store a connection's status. Secret detail values (api_key,
        webhook_secret) are ENCRYPTED before they reach the database
        — never plaintext. Raises SaasError when encryption is not
        configured for a file store (fail closed)."""
        now = _iso(_now())
        with self._lock, self._conn:
            if detail is None:
                self._conn.execute(
                    "UPDATE saas_connections SET status = ?, note = ?,"
                    " updated_at = ? WHERE site_id = ? AND kind = ?",
                    (status, note, now, site_id, kind))
            else:
                self._conn.execute(
                    "UPDATE saas_connections SET status = ?, note = ?,"
                    " detail = ?, updated_at = ? WHERE site_id = ? AND kind = ?",
                    (status, note, json.dumps(self._encrypt_detail(detail)),
                     now, site_id, kind))

    def disconnect_connection(self, site_id: str, kind: str) -> None:
        """Disconnect a connection: status back to not_connected and
        the stored detail (tokens included) deleted. The caller
        revokes the token at the provider FIRST
        (revoke_provider_token)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_connections SET status = 'not_connected',"
                " detail = '{}', note = ?, updated_at = ?"
                " WHERE site_id = ? AND kind = ?",
                ("Not connected.", _iso(_now()), site_id, kind))

    # -- conversions (paying customers) --------------------------------------
    def record_conversion(self, site_id: str, amount_cents: int,
                          currency: str, source: str,
                          stripe_event_id: str | None = None,
                          attribution: str = "unattributed",
                          signature_tag: str = "",
                          action_id: str = "") -> dict | None:
        """Record one paying customer. Returns None when the same Stripe
        event was already recorded (webhooks redeliver).
        `attribution` is "ventoryn" only when the Ventoryn signature
        reached the payment (see handle_customer_sale_webhook);
        otherwise "unattributed" — never guessed."""
        if stripe_event_id:
            with self._lock:
                seen = self._conn.execute(
                    "SELECT 1 FROM saas_conversions WHERE stripe_event_id = ?",
                    (stripe_event_id,)).fetchone()
            if seen:
                return None
        conv_id = _new_id()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO saas_conversions (id, site_id, amount_cents,"
                " currency, source, stripe_event_id, attribution,"
                " signature_tag, action_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conv_id, site_id, amount_cents, currency, source,
                 stripe_event_id, attribution, signature_tag, action_id,
                 _iso(_now())))
        return {"id": conv_id, "site_id": site_id,
                "amount_cents": amount_cents, "currency": currency,
                "source": source, "attribution": attribution,
                "signature_tag": signature_tag, "action_id": action_id}

    def list_conversions(self, site_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saas_conversions WHERE site_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (site_id, limit)).fetchall()
        return [_row_dict(r) for r in rows]

    def paying_customer_count(self, site_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM saas_conversions WHERE site_id = ?",
                (site_id,)).fetchone()
        return int(row[0])

    def revenue_for_site(self, site_id: str) -> dict | None:
        """Revenue + count + detail rows for one site's report/funnel
        — or None when the site does not exist. The three display
        settings are respected separately: revenue_show OFF hides
        every dollar figure (the total and the per-sale amounts —
        amounts are stripped from the rows themselves, so they cannot
        leak through a template); count_show OFF hides the
        paying-customer count; details_show OFF hides the per-sale
        rows entirely. A hidden value is returned as None / [] —
        never a figure the customer chose to hide. The sales are
        still recorded either way."""
        site = self.get_site(site_id)
        if not site:
            return None
        count = self.paying_customer_count(site_id)
        connection = self.get_connection(site_id, "stripe_sales")
        show_count = bool(site.get("count_show", 1))
        show_revenue = bool(site.get("revenue_show"))
        show_details = bool(site.get("details_show", 1))
        stripe_connected = bool(
            connection and connection.get("status") == "connected")
        conversions: list[dict] = []
        revenue = None
        if show_details:
            conversions = self.list_conversions(site_id)
            if not show_revenue:
                # Amounts are revenue figures: strip them here so a
                # template cannot render them by accident.
                conversions = [dict(c, amount_cents=None)
                               for c in conversions]
        if show_revenue and stripe_connected:
            all_conversions = self.list_conversions(site_id)
            total = sum(int(c["amount_cents"]) for c in all_conversions)
            currency = (all_conversions[0]["currency"]
                        if all_conversions else "usd")
            revenue = {"total_cents": total, "currency": currency}
        return {"paying_customers": count if show_count else None,
                "revenue": revenue,
                "conversions": conversions}

    def conversion_status_counts(self, site_ids: list[str]) -> dict:
        """Verified / pending / unattributed paying customers across
        the given sites — the Conversion Status breakdown.
        Verified = the Ventoryn signature reached the payment
        (attribution "ventoryn"). Pending = owner-recorded sales
        (source "manual") that no Stripe event has verified yet.
        Unattributed = a real Stripe payment with no signature.
        Never guessed: the three buckets are exactly the recorded
        states."""
        counts = {"verified": 0, "pending": 0, "unattributed": 0,
                  "total": 0}
        for site_id in site_ids:
            for c in self.list_conversions(site_id, limit=1000):
                counts["total"] += 1
                if c.get("attribution") == "ventoryn":
                    counts["verified"] += 1
                elif c.get("source") == "manual":
                    counts["pending"] += 1
                else:
                    counts["unattributed"] += 1
        return counts

    def verified_trend(self, site_ids: list[str],
                       days: int = 30) -> list[dict]:
        """Cumulative verified paying customers by day, oldest first:
        [{"date": "2026-11-01", "verified": 3}, ...] over the last
        `days` days. Real conversion dates only — a day with no
        verified sale carries the previous total, and an empty
        account returns []. Dates are UTC throughout: conversions
        are timestamped in UTC (see `_now`), so "today" is the UTC
        date, or a sale made after 19:00 EST would land in
        tomorrow's bucket and the last point would read 0."""
        today = _now().date()
        start = today - timedelta(days=days)
        by_day: dict[str, int] = {}
        for site_id in site_ids:
            for c in self.list_conversions(site_id, limit=1000):
                if c.get("attribution") != "ventoryn":
                    continue
                try:
                    day = datetime.fromisoformat(
                        c["created_at"]).date()
                except (ValueError, TypeError):
                    continue
                if day >= start:
                    by_day[day.isoformat()] = by_day.get(
                        day.isoformat(), 0) + 1
        if not by_day:
            return []
        points: list[dict] = []
        running = 0
        day = start
        while day <= today:
            running += by_day.get(day.isoformat(), 0)
            points.append({"date": day.isoformat(), "verified": running})
            day += timedelta(days=1)
        return points

    def verified_in_last_days(self, site_ids: list[str],
                              days: int = 7) -> int:
        """Verified paying customers in the last `days` days (the
        "+N verified customers (7 days)" pill)."""
        cutoff = _now() - timedelta(days=days)
        n = 0
        for site_id in site_ids:
            for c in self.list_conversions(site_id, limit=1000):
                if c.get("attribution") != "ventoryn":
                    continue
                try:
                    when = datetime.fromisoformat(c["created_at"])
                except (ValueError, TypeError):
                    continue
                if when >= cutoff:
                    n += 1
        return n

    def admin_revenue_totals(self) -> dict:
        """Platform totals for /admin. Dollar figures include ONLY
        sites whose revenue toggle is ON (and whose Stripe sales is
        connected); the paying-customer count includes ONLY sites
        whose count toggle is ON. A site with either toggle OFF
        contributes nothing to that figure — no dollar figure and no
        count from it appears anywhere on Ventoryn."""
        total_cents = 0
        currency = "usd"
        paying = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.revenue_show, s.count_show, c.status"
                " FROM saas_sites s"
                " LEFT JOIN saas_connections c ON c.site_id = s.id"
                " AND c.kind = 'stripe_sales'").fetchall()
        for row in rows:
            site_id = row[0]
            if row[2]:
                paying += self.paying_customer_count(site_id)
            if row[1] and row[3] == "connected":
                conversions = self.list_conversions(site_id)
                total_cents += sum(int(c["amount_cents"]) for c in conversions)
                if conversions:
                    currency = conversions[0]["currency"]
        return {"total_cents": total_cents, "currency": currency,
                "paying_customers": paying}

    # -- Ventoryn signature records ----------------------------------------
    def record_signature(self, site_id: str, tag: str, action_id: str,
                         agent: str, job_id: str, target_url: str) -> dict:
        """Record a signed link when it is created: what it points
        to, which agent, which job, when. The tag is what a payment
        later carries to prove attribution."""
        sig_id = _new_id()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO saas_signatures (id, site_id, tag, action_id,"
                " agent, job_id, target_url, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sig_id, site_id, tag, action_id, agent, job_id,
                 target_url, _iso(_now())))
        return {"id": sig_id, "site_id": site_id, "tag": tag,
                "action_id": action_id, "agent": agent, "job_id": job_id,
                "target_url": target_url}

    def get_signature_by_tag(self, site_id: str, tag: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_signatures WHERE site_id = ? AND tag = ?",
                (site_id, tag)).fetchone()
        return _row_dict(row) if row else None

    def list_signatures(self, site_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saas_signatures WHERE site_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (site_id, limit)).fetchall()
        return [_row_dict(r) for r in rows]

    # -- Stripe webhook idempotency ------------------------------------------
    def record_stripe_event(self, event_id: str) -> bool:
        """True the first time an event id is seen, False on redelivery."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT 1 FROM saas_stripe_events WHERE event_id = ?",
                (event_id,)).fetchone()
            if row:
                return False
            self._conn.execute(
                "INSERT INTO saas_stripe_events (event_id, processed_at)"
                " VALUES (?, ?)", (event_id, _iso(_now())))
        return True

    def stripe_sales_secrets(self) -> list[tuple[str, str]]:
        """(site_id, webhook_secret) for every site that connected its
        own Stripe webhook. Secrets are DECRYPTED here for the
        signature check only — never logged or rendered."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT site_id, detail FROM saas_connections"
                " WHERE kind = 'stripe_sales' AND status = 'connected'"
            ).fetchall()
        out = []
        for site_id, detail in rows:
            raw = (json.loads(detail or "{}").get("webhook_secret") or "")
            secret = self._decrypt_secret(raw) if raw else ""
            if secret:
                out.append((site_id, secret))
        return out

    # -- admin sessions (password login for /admin) -------------------------
    def create_admin_session(self) -> str:
        """Create a 24h admin session. Returns the raw token — only
        its hash is stored, like the Google sessions."""
        token = secrets.token_urlsafe(32)
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO saas_admin_sessions (token_hash, created_at,"
                " expires_at) VALUES (?, ?, ?)",
                (_hash_token(token), _iso(now),
                 _iso(now + timedelta(hours=ADMIN_SESSION_TTL_HOURS))))
        return token

    def check_admin_session(self, token: str) -> bool:
        """True for a live admin session. Sliding: a valid check
        extends the expiry by another 24 hours."""
        if not token:
            return False
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT token_hash FROM saas_admin_sessions"
                " WHERE token_hash = ? AND expires_at > ?",
                (_hash_token(token), _iso(now))).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE saas_admin_sessions SET expires_at = ?"
                " WHERE token_hash = ?",
                (_iso(now + timedelta(hours=ADMIN_SESSION_TTL_HOURS)),
                 _hash_token(token)))
            self._conn.commit()
        return True

    def delete_admin_session(self, token: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM saas_admin_sessions WHERE token_hash = ?",
                (_hash_token(token),))

    def get_admin_session(self, token: str) -> dict | None:
        """The admin session row (2FA-verified flag, expiry). The
        admin session is SEPARATE from the Google customer session —
        a signed-in Google session never opens /admin."""
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_admin_sessions WHERE token_hash = ?",
                (_hash_token(token),)).fetchone()
        return _row_dict(row) if row else None

    def mark_admin_session_totp_verified(self, token: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_admin_sessions SET totp_verified = 1"
                " WHERE token_hash = ?", (_hash_token(token),))

    # -- admin login rate limiting -----------------------------------------
    def record_admin_attempt(self, key: str, success: bool) -> None:
        """Record one admin login attempt from `key` (client address).
        A success clears the failure count; a failure adds one."""
        now = _iso(_now())
        with self._lock, self._conn:
            if success:
                self._conn.execute(
                    "DELETE FROM saas_admin_attempts WHERE key = ?", (key,))
                return
            row = self._conn.execute(
                "SELECT failures FROM saas_admin_attempts WHERE key = ?",
                (key,)).fetchone()
            failures = (int(row[0]) + 1) if row else 1
            locked_until = ""
            if failures >= ADMIN_LOGIN_MAX_FAILURES:
                locked_until = _iso(
                    _now() + timedelta(minutes=ADMIN_LOGIN_LOCKOUT_MINUTES))
            self._conn.execute(
                "INSERT INTO saas_admin_attempts (key, failures, locked_until)"
                " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
                " failures = excluded.failures,"
                " locked_until = excluded.locked_until",
                (key, failures, locked_until))

    def admin_login_locked(self, key: str) -> bool:
        """True when `key` is locked out after too many wrong guesses."""
        with self._lock:
            row = self._conn.execute(
                "SELECT locked_until FROM saas_admin_attempts WHERE key = ?",
                (key,)).fetchone()
        if not row or not row[0]:
            return False
        try:
            return _now() < datetime.fromisoformat(row[0])
        except ValueError:
            return False

    # -- admin two-factor (same TOTP module as customer accounts) ---------
    # The admin has no account row, so its 2FA state lives in the
    # single-row saas_admin_totp table. Same flow as customers:
    # begin setup -> confirm with a code -> enabled. Until 2FA is
    # enabled, password-only login still works (so the build itself
    # never locks the operator out); once enabled, the code is
    # required at every login.
    def admin_totp_state(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_admin_totp WHERE id = 1").fetchone()
        if not row:
            return {"enabled": False, "has_pending": False}
        d = _row_dict(row)
        return {"enabled": bool(d.get("enabled")),
                "has_pending": bool(d.get("pending_secret"))}

    def begin_admin_totp_setup(self) -> dict:
        """Start admin 2FA setup. Returns the PLAINTEXT secret and
        backup codes — shown ONCE on the setup page, never stored
        in plaintext."""
        secret = new_secret()
        salt = secrets.token_hex(8)
        codes = new_backup_codes()
        hashes = [hash_backup_code(c, salt) for c in codes]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO saas_admin_totp (id, pending_secret, salt,"
                " backup_hashes, updated_at) VALUES (1, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET pending_secret = excluded.pending_secret,"
                " salt = excluded.salt, backup_hashes = excluded.backup_hashes,"
                " updated_at = excluded.updated_at",
                (self._encrypt_secret(secret), salt, json.dumps(hashes),
                 _iso(_now())))
        return {"secret": secret, "backup_codes": codes}

    def confirm_admin_totp_setup(self, code: str) -> bool:
        """Complete admin 2FA setup with a code from the pending
        secret. Returns False on a wrong code (setup stays pending)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_admin_totp WHERE id = 1").fetchone()
        if not row or not row["pending_secret"]:
            return False
        try:
            secret = self._decrypt_secret(row["pending_secret"])
        except SaasError:
            return False
        if not verify_code(secret, code):
            return False
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_admin_totp SET secret_encrypted = ?,"
                " pending_secret = '', enabled = 1, updated_at = ?"
                " WHERE id = 1",
                (row["pending_secret"], _iso(_now())))
        return True

    def verify_admin_totp_code(self, code: str) -> bool:
        """True for a valid current TOTP code for the admin's ACTIVE
        secret. Backup codes are NOT accepted here."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_admin_totp WHERE id = 1").fetchone()
        if not row or not row["enabled"] or not row["secret_encrypted"]:
            return False
        try:
            secret = self._decrypt_secret(row["secret_encrypted"])
        except SaasError:
            return False
        return verify_code(secret, code)

    def consume_admin_backup_code(self, code: str) -> bool:
        """True (and the code is burned) for an unused admin backup code."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_admin_totp WHERE id = 1").fetchone()
        if not row or not row["backup_hashes"]:
            return False
        try:
            stored = json.loads(row["backup_hashes"])
        except (ValueError, TypeError):
            return False
        hit = verify_backup_code(code, row["salt"] or "", stored)
        if hit is None:
            return False
        stored = [h for h in stored if h != hit]
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_admin_totp SET backup_hashes = ? WHERE id = 1",
                (json.dumps(stored),))
        return True

    def get_admin_totp_pending_secret(self) -> str:
        """The PLAINTEXT pending admin secret for a setup in progress
        (so the setup page can re-show its QR code if the operator
        navigates away mid-setup). Empty when no setup is pending."""
        with self._lock:
            row = self._conn.execute(
                "SELECT pending_secret FROM saas_admin_totp WHERE id = 1"
            ).fetchone()
        if not row or not row[0]:
            return ""
        try:
            return self._decrypt_secret(row[0])
        except SaasError:
            return ""

    def regenerate_admin_backup_codes(self) -> list[str]:
        """Generate a FRESH set of admin backup codes (replacing any
        existing ones) and return the plaintext codes — the caller
        shows them ONCE (on the setup-success page) and never stores
        them in plaintext."""
        codes = new_backup_codes()
        salt = secrets.token_hex(8)
        hashes = [hash_backup_code(c, salt) for c in codes]
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_admin_totp SET salt = ?, backup_hashes = ?"
                " WHERE id = 1",
                (salt, json.dumps(hashes)))
        return codes

    def reset_admin_totp(self) -> None:
        """Operator reset of the ADMIN 2FA itself (only reachable
        from inside an admin session): wipes the secret and the
        enabled flag, so the next admin login is password-only until
        2FA is set up again."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saas_admin_totp SET secret_encrypted = '',"
                " pending_secret = '', enabled = 0, backup_hashes = '[]'"
                " WHERE id = 1")

    # -- activity: who is live now, and what happened recently --------------
    # Honest presence, never invented: last_seen is stamped only when
    # a signed-in account actually makes a request, and the live list
    # is a 5-minute window on it. Events are recorded only where the
    # thing really happened (sign-in, site added, subscribed, job run).
    def touch_activity(self, account_id: str, action: str) -> None:
        """Stamp an account as seen now, doing `action`. Writes are
        throttled to one per minute per account — the Live Ops page
        polls every 5 seconds, and presence does not need per-poll
        precision."""
        now = _iso(_now())
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seen_at FROM saas_activity WHERE account_id = ?",
                (account_id,)).fetchone()
            if row is not None:
                try:
                    last = datetime.fromisoformat(row[0])
                    if (_now() - last) < timedelta(seconds=60):
                        return
                except ValueError:
                    pass
            self._conn.execute(
                "INSERT INTO saas_activity (account_id, last_seen_at,"
                " last_action) VALUES (?, ?, ?)"
                " ON CONFLICT(account_id) DO UPDATE SET"
                " last_seen_at = excluded.last_seen_at,"
                " last_action = excluded.last_action",
                (account_id, now, action))
            self._conn.commit()

    def record_activity_event(self, account_id: str, kind: str,
                              detail: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO saas_activity_events (id, account_id, kind,"
                " detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (_new_id(), account_id, kind, detail, _iso(_now())))

    def live_accounts(self, within_minutes: int = LIVE_WINDOW_MINUTES
                      ) -> list[dict]:
        """Accounts seen within the window, newest first — email,
        name, last action, last seen. Nobody is invented: an account
        appears only after a real request stamped it."""
        cutoff = _iso(_now() - timedelta(minutes=within_minutes))
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.email, a.name, s.last_seen_at, s.last_action"
                " FROM saas_activity s JOIN saas_accounts a"
                " ON a.id = s.account_id"
                " WHERE s.last_seen_at >= ? ORDER BY s.last_seen_at DESC",
                (cutoff,)).fetchall()
        return [_row_dict(r) for r in rows]

    def recent_activity_events(self, limit: int = 30) -> list[dict]:
        """Latest activity events, newest first, with the account's
        email attached. Only real events are ever recorded."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.kind, e.detail, e.created_at, a.email"
                " FROM saas_activity_events e JOIN saas_accounts a"
                " ON a.id = e.account_id"
                " ORDER BY e.created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [_row_dict(r) for r in rows]


def _row_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}

# ---------------------------------------------------------------------------
# Google OAuth (sign-in only) — stdlib urllib, injectable urlopen for tests.
# The token exchange is server-to-server; the access token is used once
# to fetch the userinfo profile from Google, which is what validates it.
# No id_token JWT verification is needed on that path. The client id and
# secret come from the environment and are never logged.
# ---------------------------------------------------------------------------

def google_auth_url(config: SaasConfig, state: str) -> str:
    params = urllib.parse.urlencode({
        "client_id": config.google_client_id,
        "redirect_uri": config.google_redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    })
    return f"{GOOGLE_AUTH_URL}?{params}"


def _http_post_form(url: str, fields: dict, headers: dict | None = None,
                   urlopen=None) -> dict:
    opener = urlopen or urllib.request.urlopen
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=headers or {}, method="POST")
    try:
        with opener(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise SaasError(f"Could not reach the sign-in service ({exc})")


def _http_get_json(url: str, headers: dict | None = None,
                   urlopen=None) -> dict:
    opener = urlopen or urllib.request.urlopen
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with opener(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}


def exchange_google_code(config: SaasConfig, code: str,
                         urlopen=None) -> dict:
    """Exchange an authorization code for tokens. Raises SaasError on
    any failure — the caller shows a plain error page, never the detail."""
    body = _http_post_form(GOOGLE_TOKEN_URL, {
        "code": code,
        "client_id": config.google_client_id,
        "client_secret": config.google_client_secret,
        "redirect_uri": config.google_redirect_uri,
        "grant_type": "authorization_code",
    }, urlopen=urlopen)
    if body.get("error") or not body.get("access_token"):
        raise SaasError("Google did not accept the sign-in. Try again.")
    return body


def google_userinfo(access_token: str, urlopen=None) -> dict:
    """Fetch the profile for an access token from Google. The sub is
    the stable account id; email is what the customer sees."""
    profile = _http_get_json(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        urlopen=urlopen)
    if not profile.get("sub") or not profile.get("email"):
        raise SaasError("Google did not return a profile. Try again.")
    return {"sub": profile["sub"], "email": profile["email"],
            "name": profile.get("name", "")}


# ---------------------------------------------------------------------------
# Stripe — stdlib urllib + hmac, injectable urlopen for tests. No live
# keys anywhere in the repo; tests use fakes. The webhook signature
# check fails closed: anything unverifiable is rejected with 400.
# ---------------------------------------------------------------------------

def stripe_auth_header(secret_key: str) -> dict:
    token = base64.b64encode(f"{secret_key}:".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def create_checkout_session(config: SaasConfig, account: dict,
                            urlopen=None, site_url: str | None = None) -> dict:
    """Create a $20/month subscription checkout session for an account.
    Returns {"id", "url"}. Raises SaasError when Stripe is not
    configured or the call fails. When ``site_url`` is given, the
    success URL carries it, so the customer lands back on their
    dashboard with the site they typed already filled in."""
    if not config.stripe_configured:
        raise SaasError(
            "Payments are not set up yet — the price is $20/month, but "
            "the Stripe connection is still being set up.")
    success_url = f"{config.base_url}/account/dashboard?subscribed=1"
    if site_url:
        success_url += ("&site_url=" +
                        urllib.parse.quote(site_url, safe=""))
    fields = {
        "mode": "subscription",
        "line_items[0][price]": config.stripe_price_id,
        "line_items[0][quantity]": "1",
        "client_reference_id": account["id"],
        "customer_email": account["email"],
        "success_url": success_url,
        "cancel_url": f"{config.base_url}/account/subscribe?cancelled=1",
    }
    body = _http_post_form(
        STRIPE_CHECKOUT_SESSIONS_URL, fields,
        headers=stripe_auth_header(config.stripe_secret_key),
        urlopen=urlopen)
    if body.get("error") or not body.get("url"):
        message = (body.get("error") or {}).get("message") or \
            "Stripe did not create a checkout session."
        raise SaasError(f"Could not start checkout: {message}")
    return {"id": body["id"], "url": body["url"]}


def verify_webhook_signature(payload: bytes, header: str, secret: str,
                             now_s: int | None = None) -> bool:
    """Verify a Stripe webhook signature header (t=...,v1=...).
    True only when a v1 signature matches and the timestamp is fresh.
    Anything malformed or stale is False — fail closed."""
    if not payload or not header or not secret:
        return False
    try:
        parts = dict(item.split("=", 1)
                     for item in header.split(",") if "=" in item)
        timestamp = int(parts.get("t", "0"))
        signatures = [v for k, v in
                      (item.split("=", 1) for item in header.split(",")
                       if "=" in item) if k == "v1"]
    except (ValueError, TypeError):
        return False
    if not signatures or timestamp <= 0:
        return False
    now_s = int(time.time()) if now_s is None else now_s
    if abs(now_s - timestamp) > WEBHOOK_TIMESTAMP_TOLERANCE_S:
        return False
    signed = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(secret.encode("utf-8"), signed,
                        hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in signatures)


def handle_platform_webhook(store: SaasStore, config: SaasConfig,
                            event: dict) -> str:
    """Apply one Ventoryn-subscription webhook event. Returns "ok",
    "duplicate" (redelivery — already processed), or "ignored"
    (an event this shell does not act on). Grants the monthly credit
    allowance exactly once per checkout and once per renewal invoice."""
    event_id = event.get("id") or ""
    if not event_id:
        return "ignored"
    if not store.record_stripe_event(event_id):
        return "duplicate"
    etype = event.get("type") or ""
    data = event.get("data") or {}
    obj = data.get("object") or {}

    if etype == "checkout.session.completed":
        account = store.get_account(obj.get("client_reference_id") or "")
        if account is None:
            return "ignored"
        store.set_stripe_customer(
            account["id"], obj.get("customer") or "",
            obj.get("subscription"))
        store.set_subscription(account["id"], "active",
                               obj.get("subscription"))
        store.grant_credits(account["id"], config.monthly_credits,
                            "Monthly credit allowance — subscription started")
        store.record_activity_event(
            account["id"], "subscribed",
            f"{PLAN_PRICE_LABEL} subscription started")
        return "ok"

    if etype in ("customer.subscription.created",
                 "customer.subscription.updated",
                 "customer.subscription.deleted"):
        account = store.get_account_by_subscription(obj.get("id") or "")
        if account is None and obj.get("customer"):
            account = store.get_account_by_stripe_customer(obj["customer"])
        if account is None:
            return "ignored"
        period_end = obj.get("current_period_end")
        period_end_iso = _iso(datetime.fromtimestamp(period_end, timezone.utc)
                              ) if isinstance(period_end, (int, float)) else None
        store.set_subscription(account["id"], obj.get("status") or "active",
                               obj.get("id"), period_end_iso)
        return "ok"

    if etype == "invoice.payment_succeeded":
        # Renewals only: the first invoice of a new subscription is
        # already covered by the checkout grant above.
        if obj.get("billing_reason") != "subscription_cycle":
            return "ignored"
        account = None
        if obj.get("subscription"):
            account = store.get_account_by_subscription(obj["subscription"])
        if account is None and obj.get("customer"):
            account = store.get_account_by_stripe_customer(obj["customer"])
        if account is None:
            return "ignored"
        store.grant_credits(account["id"], config.monthly_credits,
                            "Monthly credit allowance — renewal")
        return "ok"

    return "ignored"


def handle_customer_sale_webhook(store: SaasStore, site_id: str,
                                 event: dict) -> str:
    """Apply one customer-site sale event (the customer's own Stripe
    account, verified against that site's stored webhook secret).
    The sale is attributed only when the event carries
    metadata.ventoryn_site_id matching the site AND the Ventoryn
    signature tag (metadata.ventoryn_tag, or utm_content) matches a
    link Ventoryn recorded for this site. A payment without the tag
    is recorded as unattributed revenue — never assigned to a post
    by guesswork. Returns "ok", "duplicate", or "ignored"."""
    event_id = event.get("id") or ""
    if not event_id:
        return "ignored"
    if not store.record_stripe_event(event_id):
        return "duplicate"
    etype = event.get("type") or ""
    data = event.get("data") or {}
    obj = data.get("object") or {}
    metadata = obj.get("metadata") or {}
    if metadata.get("ventoryn_site_id") != site_id:
        return "ignored"
    amount = None
    if etype == "checkout.session.completed":
        amount = obj.get("amount_total")
    elif etype == "payment_intent.succeeded":
        amount = obj.get("amount_received")
    elif etype == "invoice.payment_succeeded":
        amount = obj.get("amount_paid")
    if not isinstance(amount, int) or amount <= 0:
        return "ignored"
    currency = (obj.get("currency") or "usd").lower()
    tag = metadata.get("ventoryn_tag") or metadata.get("utm_content") or ""
    attribution = "unattributed"
    action_id = ""
    if tag:
        recorded = store.get_signature_by_tag(site_id, tag)
        if recorded is not None:
            attribution = "ventoryn"
            action_id = recorded["action_id"]
    recorded = store.record_conversion(
        site_id, amount, currency, "stripe", stripe_event_id=event_id,
        attribution=attribution, signature_tag=tag, action_id=action_id)
    return "ok" if recorded else "duplicate"


def check_posting_allowed(store: SaasStore, account: dict, site_id: str,
                          kind: str, config: "SaasConfig"
                          ) -> tuple[bool, str]:
    """The posting gate: can an agent post/reply on `kind` for this
    account's site? Returns (allowed, plain-language reason).
    Blocked when posting is paused for the account, when the
    connection is not connected, or when the provider needs an app
    registration the operator has not done yet. This is the check
    every posting path must call before it drafts or sends."""
    if account.get("posting_paused"):
        return False, "Posting is paused for this account."
    connection = store.get_connection(site_id, kind)
    if not connection or connection.get("status") != "connected":
        if kind in APP_REGISTRATION_KINDS and not config.app_registered(kind):
            return False, (
                f"Not connected — a {kind.capitalize()} app registration "
                "is needed before Ventoryn can post or reply as you.")
        return False, "Not connected — connect this account first."
    return True, "Connected — posting is allowed (drafts first, your approval before anything ships)."


def revoke_provider_token(kind: str, detail: dict,
                          client_id: str = "", client_secret: str = "",
                          urlopen=None) -> tuple[bool, str]:
    """Revoke a provider token on Disconnect. Returns (revoked, note).
    Reddit/X are revoked with a real HTTP call to the provider when
    the app credentials are available; when they are not (no app
    registration yet) there is no live token to revoke, so the
    disconnect is still safe. Plausible keys and Stripe webhook
    secrets have no provider revoke API — the token is deleted
    locally and the customer is told to remove it in the provider's
    dashboard (see CONNECTION_REVOKE_NOTE)."""
    if kind == "reddit":
        token = (detail or {}).get("access_token") or ""
        if not token or not client_id:
            return True, ("Nothing to revoke at Reddit — no live token "
                          "was stored.")
        return _revoke_http(
            "https://www.reddit.com/api/v1/revoke_token",
            {"token": token, "token_type_hint": "access_token"},
            client_id, client_secret, urlopen)
    if kind == "x":
        token = (detail or {}).get("access_token") or ""
        if not token or not client_id:
            return True, ("Nothing to revoke at X — no live token "
                          "was stored.")
        return _revoke_http(
            "https://api.twitter.com/2/oauth2/revoke",
            {"token": token, "token_type_hint": "access_token"},
            client_id, client_secret, urlopen)
    return True, CONNECTION_REVOKE_NOTE.get(kind, "")


def _revoke_http(url: str, fields: dict, client_id: str,
                 client_secret: str, urlopen=None) -> tuple[bool, str]:
    """POST a token revocation (HTTP Basic with the app credentials).
    Returns (revoked, note). A failed call is reported, not hidden —
    the caller still deletes the local copy, and the note says the
    provider revoke did not confirm."""
    opener = urlopen or urllib.request.urlopen
    data = urllib.parse.urlencode(fields).encode("utf-8")
    auth = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        url, data=data, headers={"Authorization": f"Basic {auth}"},
        method="POST")
    try:
        with opener(request) as response:
            ok = 200 <= response.status < 300
    except Exception:
        ok = False
    if ok:
        return True, "The token was revoked at the provider."
    return False, ("The provider did not confirm the revocation — the "
                   "token was deleted here; remove it in the provider's "
                   "dashboard to be sure.")


# ---------------------------------------------------------------------------
# Plausible — real visitor counts for a customer site, or None (which the
# dashboard renders as "unavailable"). Any failure is None, never a
# zero and never an exception.
# ---------------------------------------------------------------------------

def plausible_visitors(api_key: str, site_domain: str,
                       urlopen=None) -> int | None:
    if not api_key or not site_domain:
        return None
    params = urllib.parse.urlencode({
        "site_id": site_domain, "period": "30d", "metrics": "visitors",
    })
    try:
        result = _http_get_json(
            f"https://plausible.io/api/v1/stats/aggregate?{params}",
            headers={"X-Api-Key": api_key}, urlopen=urlopen)
        value = (result.get("results") or {}).get("visitors", {}).get("value")
        return int(value) if isinstance(value, (int, float)) else None
    except Exception:
        return None
