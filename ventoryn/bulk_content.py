"""Bulk content generation: many assets per brand in one run.

One bulk run takes a site + an asset mix (N social posts, M ads,
K emails) and produces that many drafts in one go — closing the gap
with Holo, which generates hundreds of assets per brand per run
while Axionory only drafted one campaign at a time.

Rules, same as everywhere else in Axionory:

- **Allowance is a CAP, not a currency** (``ventoryn/plans.py``):
  per-run and per-month maxima per tier. Exceeding either is
  rejected BEFORE any work or wallet charge happens.
- **Wallet**: usage draws from the prepaid wallet through the SAME
  metering path single generations use — reserve, ``collecting_usage``
  + ``persist_usage_events``, ``price_and_settle_job`` — one code
  path, no parallel accounting. The wallet is charged exactly once
  per run (one settle), never for a rejected run.
- **Draft-only**: everything a run generates stays DRAFT (status
  ``pending``) until a human approves it through the existing
  approval workflow (``campaigns.propose`` / ``schedule`` /
  ``reject``, Approvals queue). A bulk run never schedules, never
  posts, never sends.
- **Per site/tenant**: runs and drafts are stored per site in the
  SaasStore; the monthly cap is counted per account.

Generation reuses the existing paths: social posts go through
``campaigns.draft_posts`` + ``campaigns.propose``. There is no
existing ad or email generator, so ``_draft_ads`` / ``_draft_emails``
follow the ``draft_posts`` pattern exactly (same gateway, same
JSON-only contract, same fact-grounding on the site's own pages,
same brand-voice shaping) instead of inventing a second one.

``brand_profile`` is the site's Brand DNA in Gap 1's exact shape
(see ``ventoryn.brand_dna.extract_profile``): when present, its
non-empty fields shape every generator's voice; when absent,
generation is exactly as before. This module never extracts a
profile — the stored-profile load + call-site wiring lands when
Gap 1 merges.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ventoryn import campaigns
from ventoryn.plans import plan_limits
from ventoryn.pricing import BULK_ASSET_RESERVE_ESTIMATE_CENTS
from ventoryn.usage_metering import collecting_usage, persist_usage_events

__all__ = ["BulkError", "BulkNotAvailable", "BulkOverAllowance",
           "BulkRunStore", "bulk_run_store_for", "run_bulk",
           "validate_mix", "KINDS"]

#: Asset kinds a bulk run can produce, in the order they are saved.
KINDS = ("social", "ad", "email")

#: One LLM call per batch of this many assets (social batches are
#: capped by campaigns.MAX_POSTS instead). A 100-asset run is many
#: small calls, not one giant JSON blob.
_AD_EMAIL_BATCH = 25

SCHEMA = """
CREATE TABLE IF NOT EXISTS saas_bulk_runs (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL,
    mix TEXT NOT NULL,
    assets INTEGER NOT NULL DEFAULT 0,
    campaign_ids TEXT NOT NULL DEFAULT '[]',
    job_id TEXT NOT NULL DEFAULT '',
    wallet_charged_cents INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
"""


class BulkError(Exception):
    """A bulk run could not start or could not finish."""


class BulkNotAvailable(BulkError):
    """Bulk runs are a tier feature — legacy accounts have no bulk."""


class BulkOverAllowance(BulkError):
    """The run would exceed the plan's per-run or monthly cap."""


def validate_mix(mix) -> dict[str, int]:
    """A mix is ``{"social": N, "ad": M, "email": K}``: ints >= 0,
    at least one asset in total. Unknown kinds are rejected — a
    typo must fail loudly, never silently generate nothing."""
    if not isinstance(mix, dict):
        raise ValueError("mix must be a dict like {'social': 5, 'ad': 2}")
    clean: dict[str, int] = {}
    for kind, count in mix.items():
        if kind not in KINDS:
            raise ValueError(f"unknown bulk asset kind: {kind!r}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"bulk count for {kind!r} must be an int >= 0")
        clean[kind] = count
    for kind in KINDS:
        clean.setdefault(kind, 0)
    if sum(clean.values()) < 1:
        raise ValueError("a bulk run needs at least one asset")
    return clean


# -- storage (mirrors BrandDnaStore: one row per run, per site) --------------

class BulkRunStore:
    def __init__(self, saas_store):
        self._conn, self._lock = saas_store._conn, saas_store._lock
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def create(self, site_id: str, account_id: str, mix: dict,
               job_id: str = "") -> dict:
        from ventoryn.saas import _iso, _new_id, _now
        run_id = _new_id()
        now = _iso(_now())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO saas_bulk_runs (id, site_id, account_id, status,"
                " mix, created_at, job_id) VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (run_id, site_id, account_id, json.dumps(mix), now, job_id))
        return self.get(run_id)

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saas_bulk_runs WHERE id = ?",
                (run_id,)).fetchone()
        if row is None:
            return None
        return _run_dict(row)

    def finish(self, run_id: str, status: str, *, assets: int | None = None,
               campaign_ids: list[str] | None = None,
               wallet_charged_cents: int = 0, error: str = "") -> None:
        from ventoryn.saas import _iso, _now
        try:
            with self._lock, self._conn:
                if assets is None:
                    self._conn.execute(
                        "UPDATE saas_bulk_runs SET status = ?, finished_at = ?,"
                        " campaign_ids = ?, wallet_charged_cents = ?, error = ?"
                        " WHERE id = ?",
                        (status, _iso(_now()), json.dumps(campaign_ids or []),
                         wallet_charged_cents, error[:300], run_id))
                else:
                    self._conn.execute(
                        "UPDATE saas_bulk_runs SET status = ?, finished_at = ?,"
                        " assets = ?, campaign_ids = ?, wallet_charged_cents = ?,"
                        " error = ? WHERE id = ?",
                        (status, _iso(_now()), assets,
                         json.dumps(campaign_ids or []),
                         wallet_charged_cents, error[:300], run_id))
        except sqlite3.ProgrammingError:
            # Same as the snapshot/brand stores: the store was closed
            # while the run was in flight. The run row is bookkeeping —
            # dropping it is a correct no-op.
            pass

    def list_for_site(self, site_id: str, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saas_bulk_runs WHERE site_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (site_id, limit)).fetchall()
        return [_run_dict(r) for r in rows]

    def monthly_assets(self, account_id: str, month: str | None = None) -> int:
        """Bulk assets in completed runs for this account in a UTC
        calendar month ('YYYY-MM', defaulting to this month). Failed
        runs never generated drafts and never count."""
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(assets), 0) FROM saas_bulk_runs"
                " WHERE account_id = ? AND status = 'done'"
                " AND substr(created_at, 1, 7) = ?",
                (account_id, month)).fetchone()
        return int(row[0]) if row else 0


def _run_dict(row) -> dict:
    d = dict(row)
    d["mix"] = json.loads(d["mix"] or "{}")
    d["campaign_ids"] = json.loads(d["campaign_ids"] or "[]")
    return d


def bulk_run_store_for(saas_store) -> BulkRunStore:
    s = getattr(saas_store, "_ventoryn_bulk_runs", None)
    if s is None:
        s = BulkRunStore(saas_store)
        saas_store._ventoryn_bulk_runs = s
    return s


# -- generation ---------------------------------------------------------------

def _load_inventory(site: dict, inventory):
    """The site's pages (for fact-grounding), exactly as draft_posts
    loads them: an injected inventory in tests, a live fetch in
    production, never a crash when the fetch fails."""
    if inventory is not None:
        return inventory
    try:
        from core.seo_content import fetch_inventory
        return fetch_inventory(site["url"], max_pages=25)
    except ValueError:
        return None


def _pages(site: dict, inventory):
    inv = _load_inventory(site, inventory)
    pages = [(p.url, getattr(p, "title", "") or "") for p in
             (inv.pages if inv else [])][:25]
    page_list = "\n".join(f"- {u}  ({t})" for u, t in pages) or f"- {site['url']}"
    return page_list, {u for u, _ in pages} | {site["url"]}


def _brand_section(brand_profile: dict | None) -> str:
    """The profile's non-empty fields as prompt voice lines — the
    same shaping draft_posts applies, so all three kinds sound like
    the same brand. No profile, no section, prompt unchanged."""
    if not brand_profile:
        return ""
    lines = []
    tone = ", ".join(brand_profile.get("tone") or [])
    products = ", ".join(p.get("name", "")
                         for p in brand_profile.get("products") or [])
    audience = ", ".join(brand_profile.get("audience") or [])
    triggers = ", ".join(brand_profile.get("buying_triggers") or [])
    if tone:
        lines.append(f"Tone of voice: {tone}.")
    if products:
        lines.append(f"Products: {products}.")
    if audience:
        lines.append(f"Who it is for: {audience}.")
    if triggers:
        lines.append(f"What makes buyers act: {triggers}.")
    if not lines:
        return ""
    return ("\n\nBrand voice — facts learned from the site itself, "
            "write in this voice:\n"
            + "\n".join(f"- {line}" for line in lines))


def _complete(llm, prompt: str, system: str) -> dict:
    """One JSON-only LLM call; ``llm`` is injected (a fake in tests),
    the real gateway in production."""
    from agents.marketing.agent import clean_llm_text
    from core.llm import LLMGateway
    gateway = llm or LLMGateway()
    result = gateway.complete(prompt, tier=getattr(LLMGateway, "SMART", "smart"),
                              system=system, json_mode=True, timeout=180)
    return clean_llm_text(result.get("json") or {}) or {}


def _draft_ads(site: dict, *, count: int, llm=None, inventory=None,
               brand_profile: dict | None = None) -> list[dict]:
    """Short ad copy: headline + body, each linking to a real page of
    the site. Raises RuntimeError when no usable ad comes back."""
    page_list, allowed = _pages(site, inventory)
    prompt = (
        f"You write short ads for the website {site['url']} "
        f"(market: {site.get('country', '')}, language: {site.get('language', 'en')}).\n"
        f"Write {count} DIFFERENT ads.\n\n"
        f"The ONLY facts you may use are these pages and their titles — never "
        f"invent prices, numbers, features, testimonials or claims:\n{page_list}"
        f"{_brand_section(brand_profile)}\n\n"
        f"Rules: each ad has a headline (at most 60 characters) and a body "
        f"(at most 200 characters), written in the site's language, helpful "
        f"and specific, no hashtag stuffing, no URLs in the text, and pick "
        f"for each ad the most relevant page above as its link.\n"
        'Return ONLY JSON: {"ads": [{"headline": "...", "body": "...", '
        '"link": "<one page URL above>"}]}')
    payload = _complete(llm, prompt, "You are a careful ad writer. JSON only.")
    out, seen = [], set()
    for item in (payload.get("ads") or []):
        if not isinstance(item, dict):
            continue
        headline = " ".join(str(item.get("headline") or "").split())
        body = " ".join(str(item.get("body") or "").split())
        if not headline or len(headline) > 60 or not body or len(body) > 200:
            continue
        key = (headline + "\n" + body).lower()
        if key in seen:
            continue
        seen.add(key)
        link = str(item.get("link") or "").strip()
        out.append({"kind": "ad", "text": f"{headline}\n{body}",
                    "link": link if link in allowed else site["url"]})
        if len(out) >= count:
            break
    if not out:
        raise RuntimeError("the agent returned no usable ads")
    return out


def _draft_emails(site: dict, *, count: int, llm=None, inventory=None,
                  brand_profile: dict | None = None) -> list[dict]:
    """Marketing emails: subject + body. No link — an email is sent,
    not posted, and its links are added by the send step, not here.
    Raises RuntimeError when no usable email comes back."""
    page_list, _allowed = _pages(site, inventory)
    prompt = (
        f"You write marketing emails for the website {site['url']} "
        f"(market: {site.get('country', '')}, language: {site.get('language', 'en')}).\n"
        f"Write {count} DIFFERENT emails.\n\n"
        f"The ONLY facts you may use are these pages and their titles — never "
        f"invent prices, numbers, features, testimonials or claims:\n{page_list}"
        f"{_brand_section(brand_profile)}\n\n"
        f"Rules: each email has a subject (at most 80 characters) and a body "
        f"(at most 1000 characters), written in the site's language, helpful "
        f"and specific, no hashtag stuffing, no URLs in the text.\n"
        'Return ONLY JSON: {"emails": [{"subject": "...", "body": "..."}]}')
    payload = _complete(llm, prompt, "You are a careful email writer. JSON only.")
    out, seen = [], set()
    for item in (payload.get("emails") or []):
        if not isinstance(item, dict):
            continue
        subject = " ".join(str(item.get("subject") or "").split())
        body = " ".join(str(item.get("body") or "").split())
        if not subject or len(subject) > 80 or not body or len(body) > 1000:
            continue
        key = (subject + "\n" + body).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": "email",
                    "text": f"Subject: {subject}\n\n{body}", "link": ""})
        if len(out) >= count:
            break
    if not out:
        raise RuntimeError("the agent returned no usable emails")
    return out


def _generate_default(site: dict, kind: str, count: int, *, llm=None,
                      inventory=None,
                      brand_profile: dict | None = None) -> list[dict]:
    """The default generator for one kind: social reuses
    ``campaigns.draft_posts`` (batched at MAX_POSTS); ads and emails
    use the module's own helpers (batched at 25)."""
    if kind == "social":
        out: list[dict] = []
        remaining = count
        while remaining > 0:
            batch = min(remaining, campaigns.MAX_POSTS)
            posts = campaigns.draft_posts(
                site, days=1, posts_per_day=batch, with_link=True,
                llm=llm, inventory=inventory, brand_profile=brand_profile)
            out.extend({"kind": "social", "text": p["text"], "link": p["link"]}
                       for p in posts)
            remaining -= batch
            if len(posts) < batch:  # generator is out of usable posts
                break
        if not out:
            raise RuntimeError("the agent returned no usable posts")
        return out[:count]
    if kind == "ad":
        out = []
        remaining = count
        while remaining > 0:
            batch = min(remaining, _AD_EMAIL_BATCH)
            out.extend(_draft_ads(site, count=batch, llm=llm,
                                  inventory=inventory,
                                  brand_profile=brand_profile))
            remaining -= batch
        return out[:count]
    out = []
    remaining = count
    while remaining > 0:
        batch = min(remaining, _AD_EMAIL_BATCH)
        out.extend(_draft_emails(site, count=batch, llm=llm,
                                 inventory=inventory,
                                 brand_profile=brand_profile))
        remaining -= batch
    return out[:count]


def _save(store, site: dict, account: dict, assets: list[dict]) -> list[dict]:
    """Save generated assets as draft campaigns, one campaign per
    kind, all status ``pending``. Social reuses ``campaigns.propose``;
    ads/emails create their campaign the same way ``propose`` does
    and add one draft row per asset. Nothing here schedules or posts
    anything — approval is a separate, human step."""
    saved = []
    by_kind: dict[str, list[dict]] = {}
    for asset in assets:
        by_kind.setdefault(asset["kind"], []).append(asset)
    for kind in KINDS:
        group = by_kind.get(kind)
        if not group:
            continue
        if kind == "social":
            campaign = campaigns.propose(
                store, site, group, days=1, posts_per_day=len(group),
                with_link=True, source="bulk")
        else:
            campaign = store.create_campaign(
                site["id"], site["account_id"], days=1,
                posts_per_day=len(group), with_link=False, source="bulk",
                platform=kind)
            for asset in group:
                store.create_social_draft(site["id"], kind, asset["text"],
                                          asset.get("link") or "",
                                          campaign_id=campaign["id"])
        saved.append(campaign)
    return saved


# -- the bulk run ---------------------------------------------------------------

def run_bulk(store, site: dict, account: dict, mix: dict, *,
             llm=None, inventory=None, brand_profile: dict | None = None,
             generate=None, is_operator: bool = False) -> dict:
    """Run one bulk generation for ``site``.

    Order matters: validate the mix, check the plan's caps, THEN
    touch the wallet (reserve + wallet job), THEN generate inside
    the usage collector and settle exactly once in the ``finally``.
    An over-allowance run raises before any reservation exists, so
    it can never be charged. A run that fails mid-generation still
    settles whatever LLM usage actually ran (real provider money),
    saves no drafts, and is recorded ``failed``.

    ``generate`` is injected for tests: ``generate(site, kind, count,
    *, llm, inventory, brand_profile) -> list[dict]``. Returns the
    run record: ``{"id", "site_id", "status": "done", "mix", "assets",
    "total_assets", "campaign_ids", "job_id",
    "wallet_charged_cents"}``.
    """
    from ventoryn import pricing

    clean_mix = validate_mix(mix)
    total = sum(clean_mix.values())
    tier = plan_limits(account.get("plan") or "")
    if tier is None:
        raise BulkNotAvailable(
            "bulk content needs a Starter, Growth or Pro plan — "
            "legacy accounts use one campaign at a time")
    per_run = tier.get("bulk_max_assets_per_run")
    if per_run is not None and total > per_run:
        raise BulkOverAllowance(
            f"this run asks for {total} assets but the "
            f"{tier['label']} plan allows at most {per_run} per run")
    per_month = tier.get("bulk_max_assets_per_month")
    runs = bulk_run_store_for(store)
    if per_month is not None:
        used = runs.monthly_assets(account["id"])
        if used + total > per_month:
            raise BulkOverAllowance(
                f"this run would take the account to {used + total} bulk "
                f"assets this month but the {tier['label']} plan allows "
                f"at most {per_month}")

    # Wallet first, generation second: the hold exists before any LLM
    # call runs, and the settle below is the run's only charge.
    reservation = None
    job = None
    if not is_operator:
        reservation = store.reserve_wallet_funds(
            account["id"], total * BULK_ASSET_RESERVE_ESTIMATE_CENTS,
            "bulk content run")
        job = store.create_wallet_job(account["id"], "bulk_content",
                                      site["id"], reservation["id"])
    run = runs.create(site["id"], account["id"], clean_mix,
                      job_id=job["id"] if job else "")
    job_id = job["id"] if job else run["id"]
    generate_fn = generate or _generate_default
    charged = 0
    campaign_ids: list[str] = []
    try:
        with collecting_usage() as events:
            try:
                assets: list[dict] = []
                for kind in KINDS:
                    count = clean_mix[kind]
                    if count:
                        assets.extend(generate_fn(
                            site, kind, count, llm=llm, inventory=inventory,
                            brand_profile=brand_profile))
                saved = _save(store, site, account, assets)
                campaign_ids = [c["id"] for c in saved]
            finally:
                # Same path single generations use: record what ran,
                # then price + settle exactly once. Idempotent on
                # both sides — a retried persist/settle never
                # double-records or double-charges.
                persist_usage_events(store, events, account_id=account["id"],
                                     job_id=job_id, site_id=site["id"],
                                     campaign_id=campaign_ids[0]
                                     if campaign_ids else "")
                if reservation is not None:
                    charged = pricing.price_and_settle_job(
                        store, job["id"], reservation["id"])
        counts = {kind: sum(1 for a in assets if a["kind"] == kind)
                  for kind in KINDS}
        runs.finish(run["id"], "done", assets=sum(counts.values()),
                    campaign_ids=campaign_ids, wallet_charged_cents=charged)
        return {"id": run["id"], "site_id": site["id"],
                "account_id": account["id"], "status": "done",
                "mix": clean_mix, "assets": counts,
                "total_assets": sum(counts.values()),
                "campaign_ids": campaign_ids, "job_id": job_id,
                "wallet_charged_cents": charged}
    except Exception as exc:  # noqa: BLE001 - recorded failed, re-raised
        runs.finish(run["id"], "failed", wallet_charged_cents=charged,
                    error=str(exc))
        raise
