"""Autopilot: the agents work on every website by default.

Every website on Ventoryn gets its agent work on a cadence, without
anyone pressing "Run job":

    Job          Cadence   Why
    analyze      7 days    site + GEO audit: what changed, what to fix
    seo_article  7 days    one SEO article draft (needs an AI key)

Rules (same as a job run by hand):
- Credits are charged exactly as for a manual run; the owner is never
  charged. A customer whose balance is too low is skipped — nothing is
  charged, nothing runs. A failed run is refunded.
- Nothing is published or posted: articles are drafts for review.
- On by default for every website; the owner of a website can turn it
  off (and back on) from that website's page.
- A failed job is retried after a day, not every tick.
- One job per tick, so the agents never crowd the server.

The loop runs in the app process (``start``), unless the environment
sets VENTORYN_AUTOPILOT=off (tests do).
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ventoryn import site_jobs
from ventoryn.saas import CREDIT_COSTS, InsufficientCredits

#: (job type, days between runs)
AUTOPILOT_JOBS: tuple[tuple[str, int], ...] = (
    ("analyze", 7), ("seo_article", 7), ("x_campaign", 7), ("video_render", 7))
RETRY_AFTER_FAILURE = timedelta(days=1)
TICK_SECONDS = 600
AUTO_PREFIX = "Automatic — "


def enabled() -> bool:
    return os.environ.get("VENTORYN_AUTOPILOT", "on").strip().lower() not in (
        "off", "0", "false", "no")


def _parse(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def due_jobs(store, site: dict, now: datetime) -> list[str]:
    """Job types due for this site now (in AUTOPILOT_JOBS order)."""
    if not int(site.get("autopilot", 1) or 0):
        return []
    due = []
    for job_type, days in AUTOPILOT_JOBS:
        if site_jobs.server_blocker(job_type):
            continue
        # One open proposal at a time: no new campaign while the owner
        # has not decided the last one.
        if job_type == "x_campaign" and store.open_campaign_for_site(site["id"]):
            continue
        # Weekly video only where the owner chose a presenter (HeyGen is a
        # paid API), and not while the last one still waits for review.
        if job_type == "video_render":
            avatar = site.get("video_avatar_id") or ""
            if not avatar or avatar == "none":
                continue
            last_video = store.last_job(site["id"], "video_render")
            if (last_video and last_video.get("status") == "done"
                    and store.video_review_status(last_video["id"]) == "pending"):
                continue
        last = store.last_job(site["id"], job_type)
        if last is None:
            due.append(job_type)
            continue
        when = _parse(last.get("created_at", "")) or now
        wait = RETRY_AFTER_FAILURE if last.get("status") == "failed" else timedelta(days=days)
        if last.get("status") in ("queued", "running"):
            continue
        if now - when >= wait:
            due.append(job_type)
    return due


def execute(store, config, account: dict, site: dict, job_type: str, *,
            orchestrator, reports_dir: Path, inputs: dict | None = None,
            job: dict | None = None) -> dict:
    """Charge (unless ``job`` was already charged), run, record —
    refund on failure. Returns the job row."""
    is_op = config.is_operator(account.get("email"))
    if job is None:
        job = store.charge_credits(account["id"], job_type, site["id"], is_operator=is_op)
    auto = not (inputs or {}).get("source") == "manual"
    prefix = AUTO_PREFIX if auto else ""
    try:
        summary, _report = site_jobs.run(job_type, site, inputs or {},
                                         orchestrator=orchestrator,
                                         reports_dir=reports_dir, store=store)
        store.set_job_result(job["id"], "done", prefix + summary)
        store.record_activity_event(
            account["id"], "job",
            f"{site_jobs.JOB_LABELS[job_type].split(' — ')[0]} ran "
            f"{'automatically ' if auto else ''}on "
            f"{site['url']}: {summary.split(' · report')[0]}")
    except Exception as exc:  # noqa: BLE001 - refund and record
        charged = job["credits_charged"]
        if charged:
            store.grant_credits(account["id"], charged,
                                f"Refund — automatic {job_type} job failed")
        store.set_job_result(job["id"], "failed", f"{prefix}failed ({exc})"
                             + (f" — refunded {charged} credits" if charged else ""))
    return store.get_job(job["id"])


#: A job still "queued" this long after it started was orphaned (the
#: server restarted mid-run): it is failed and refunded so it can retry.
STALE_AFTER = timedelta(hours=2)


def expire_stale_jobs(store, now: datetime) -> int:
    """Fail + refund jobs orphaned by a restart. Returns how many."""
    n = 0
    for job in store.stale_jobs((now - STALE_AFTER).isoformat()):
        charged = int(job.get("credits_charged") or 0)
        if charged:
            store.grant_credits(job["account_id"], charged,
                                f"Refund — {job['job_type']} job interrupted")
        store.set_job_result(job["id"], "failed",
                             "Interrupted (the server restarted mid-run)"
                             + (f" — refunded {charged} credits" if charged else ""))
        n += 1
    return n


def run_once(store, config, *, orchestrator, reports_dir: Path,
             now: datetime | None = None, max_jobs: int = 1) -> list[dict]:
    """Run up to ``max_jobs`` due jobs across all websites."""
    now = now or datetime.now(timezone.utc)
    expire_stale_jobs(store, now)
    # Approved campaigns: post whatever is due (each through the same
    # gate and X budget as a manual post).
    try:
        from ventoryn import campaigns
        campaigns.post_due(store, config, now)
    except Exception as exc:  # noqa: BLE001 - posting must not stop the agents
        print(f"autopilot: posting ERROR {exc}", flush=True)
    ran: list[dict] = []
    for site in store.all_sites():
        account = store.get_account(site["account_id"])
        if account is None:
            continue
        is_op = config.is_operator(account.get("email"))
        for job_type in due_jobs(store, site, now):
            if len(ran) >= max_jobs:
                return ran
            if not is_op and store.credit_balance(account["id"]) < CREDIT_COSTS[job_type]:
                continue
            try:
                inputs = {"source": "autopilot"}
                if job_type == "video_render":
                    inputs.update(presenter="heygen", voiceover="off",
                                  avatar_id=site.get("video_avatar_id") or "")
                ran.append(execute(store, config, account, site, job_type,
                                   orchestrator=orchestrator, reports_dir=reports_dir,
                                   inputs=inputs))
            except InsufficientCredits:
                continue
    return ran


def start(store, config, *, get_orchestrator, reports_dir: Path,
          interval: float = TICK_SECONDS) -> threading.Event | None:
    """Start the background loop (daemon thread). Returns its stop event,
    or None when autopilot is turned off."""
    if not enabled():
        return None
    stop = threading.Event()

    def loop():
        # Let the app finish starting before the first job.
        if stop.wait(60):
            return
        while not stop.is_set():
            try:
                run_once(store, config, orchestrator=get_orchestrator(),
                         reports_dir=reports_dir)
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                print(f"autopilot: ERROR {exc}", flush=True)
            stop.wait(interval)

    threading.Thread(target=loop, name="ventoryn-autopilot", daemon=True).start()
    print(f"autopilot: on — every website, one job per {int(interval)}s tick", flush=True)
    return stop
