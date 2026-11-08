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
#: through the shell today (see dashboard/saas_routes.py); the rest are
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
    #: with Google like anyone else; this email is what gates /admin,
    #: the operator tools, and ownership of the existing ecosystem
    #: businesses. Read from the environment (SAAS_OPERATOR_EMAIL) —
    #: never a hardcoded secret, just an email address. Empty means
    #: "no operator configured".
    operator_email: str = ""

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
        )

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
"""

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


class SaasStore:
    """All SaaS state. db_path is a file path, or ":memory:" for the
    hermetic default (tests and plain create_app())."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                     timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SAAS_SCHEMA)

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
        site_id = _new_id()
        now = _iso(_now())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO saas_sites (id, account_id, url, host, country,"
                " language, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (site_id, account_id, clean, urlparse(clean).netloc,
                 (country or "ca").lower()[:2],
                 (language or "en").lower()[:2], now))
            for kind, status, note in DEFAULT_CONNECTIONS:
                self._conn.execute(
                    "INSERT INTO saas_connections (id, site_id, kind, status,"
                    " note, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (_new_id(), site_id, kind, status, note, now))
        return self.get_site(site_id)

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
        d["_detail_raw"] = json.loads(d.get("detail") or "{}")
        return d

    def update_connection(self, site_id: str, kind: str, status: str,
                          note: str, detail: dict | None = None) -> None:
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
                    (status, note, json.dumps(detail), now, site_id, kind))

    # -- conversions (paying customers) --------------------------------------
    def record_conversion(self, site_id: str, amount_cents: int,
                          currency: str, source: str,
                          stripe_event_id: str | None = None) -> dict | None:
        """Record one paying customer. Returns None when the same Stripe
        event was already recorded (webhooks redeliver)."""
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
                " currency, source, stripe_event_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conv_id, site_id, amount_cents, currency, source,
                 stripe_event_id, _iso(_now())))
        return {"id": conv_id, "site_id": site_id,
                "amount_cents": amount_cents, "currency": currency,
                "source": source}

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
        own Stripe webhook. Secrets are used only for signature checks,
        never logged or rendered."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT site_id, detail FROM saas_connections"
                " WHERE kind = 'stripe_sales' AND status = 'connected'"
            ).fetchall()
        out = []
        for site_id, detail in rows:
            secret = (json.loads(detail or "{}").get("webhook_secret") or "")
            if secret:
                out.append((site_id, secret))
        return out


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
    metadata.ventoryn_site_id matching the site. Returns "ok",
    "duplicate", or "ignored"."""
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
    recorded = store.record_conversion(
        site_id, amount, currency, "stripe", stripe_event_id=event_id)
    return "ok" if recorded else "duplicate"


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
