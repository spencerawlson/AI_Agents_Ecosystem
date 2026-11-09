"""Background site-analysis jobs for the Ventoryn dashboard.

The /ops page's "Analyze a site" panel and the landing page's free
preview start a job here; the same ``ecosystem.analyze`` pipeline
the CLI runs does the work in a background thread, and the page polls
for the result. Nothing is published or posted anywhere: the only
output is the on-page summary (site score, GEO score, top findings).

Preview jobs NEVER write the full Markdown report file and never
expose one: the full report comes only from the paid Analyze job on
a website page (5 credits, ``POST /account/sites/{site_id}/jobs``).
Anonymous previews are rate-limited per client IP (see
``PREVIEW_PER_IP_PER_HOUR``) so the free endpoint cannot be hammered.

Jobs live on the app instance (``app.state.analyze_jobs``), so tests
get a fresh store per app and a restart drops in-flight jobs — the
page treats a 404 on poll as "the dashboard restarted, run it again".
The preview rate-limit counters live in the same in-memory store,
so a restart resets them too — acceptable for an abuse control,
not a billing boundary.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from core.countries import normalize_country

_MAX_JOBS = 20

#: Free-preview rate limit: at most this many anonymous previews per
#: client IP per rolling hour. A real visitor tries one or two sites;
#: 5 leaves headroom and still stops hammering. Signed-in users are
#: not limited here — their usage is attributed to an account.
PREVIEW_PER_IP_PER_HOUR = 5
_PREVIEW_WINDOW_SECONDS = 3600


def preview_client_ip(headers, peer_host: str) -> str:
    """Best-effort client IP for the preview rate limit.

    Production runs behind the Cloudflare tunnel, which sets
    ``CF-Connecting-IP`` to the real visitor — Cloudflare overwrites
    it, so a visitor cannot spoof it through the tunnel. The header
    is trusted ONLY when the TCP peer is loopback, i.e. the local
    cloudflared daemon: the app also listens on 0.0.0.0, and a caller
    reaching that port directly could otherwise set any
    CF-Connecting-IP and get a fresh allowance on every request.
    Otherwise fall back to the TCP peer. ``X-Forwarded-For`` is
    deliberately NOT trusted: any direct caller can forge it, and the
    tunnel already supplies the real IP.
    """
    import ipaddress as _ip

    def _is_loopback(host: str) -> bool:
        try:
            return _ip.ip_address((host or "").split("%", 1)[0]).is_loopback
        except ValueError:
            return False

    if not _is_loopback(peer_host):
        return (peer_host or "unknown").strip() or "unknown"
    try:
        items = headers.items() if hasattr(headers, "items") else []
    except Exception:  # noqa: BLE001 - a bare mapping is fine too
        items = []
    cf = ""
    for key, value in items:
        if str(key).lower() == "cf-connecting-ip":
            cf = value
            break
    if isinstance(cf, str) and cf.strip():
        return cf.strip()
    return (peer_host or "unknown").strip() or "unknown"


def _prune_preview_hits(store: dict, now: float) -> None:
    """Drop rate-limit entries whose hits are all outside the window
    (callers hold the store lock)."""
    for ip in list(store["preview_hits"]):
        fresh = [t for t in store["preview_hits"][ip]
                 if now - t < _PREVIEW_WINDOW_SECONDS]
        if fresh:
            store["preview_hits"][ip] = fresh
        else:
            del store["preview_hits"][ip]


def preview_allowed(store: dict, ip: str) -> tuple[bool, int]:
    """(allowed, retry_after_seconds) for one anonymous preview from
    `ip`. Records nothing — call `record_preview_hit` only after the
    job has actually started, so rejected input never burns quota."""
    now = time.monotonic()
    with store["lock"]:
        _prune_preview_hits(store, now)
        hits = store["preview_hits"].get(ip, [])
        if len(hits) >= PREVIEW_PER_IP_PER_HOUR:
            retry = int(_PREVIEW_WINDOW_SECONDS - (now - hits[0])) + 1
            return False, max(retry, 1)
        return True, 0


def record_preview_hit(store: dict, ip: str) -> None:
    """Record one started anonymous preview from `ip`."""
    with store["lock"]:
        store["preview_hits"].setdefault(ip, []).append(time.monotonic())


def _resolved_addresses(host: str, timeout: float = 3.0) -> list[str]:
    """DNS answers for a hostname, best-effort. A slow or failing
    lookup returns [] — the fetch itself then reports honestly."""
    out: list[str] = []

    def _lookup() -> None:
        try:
            for _fam, _typ, _proto, _canon, sockaddr in socket.getaddrinfo(host, None):
                out.append(sockaddr[0])
        except Exception:  # noqa: BLE001 - DNS failure is not fatal here
            pass

    t = threading.Thread(target=_lookup, daemon=True)
    t.start()
    t.join(timeout)
    return out


def _is_private_address(value: str) -> bool:
    """Internal / reserved address? One rule for the whole platform:
    core.netguard (the fetcher enforces it again at connect time)."""
    from core.netguard import is_blocked_address

    return is_blocked_address(value)


def _normalize_scheme(raw: str) -> str:
    """A bare domain works: prepend https:// when no scheme is
    present, after trimming whitespace."""
    url = raw.strip()
    if "://" not in url:
        url = "https://" + url
    return url


def validate_url(raw: object) -> str:
    """Normalize a website URL, or raise ValueError with the reason.

    A bare domain works — https:// is added when no scheme is
    present. http/https only; rejects empty input, non-URLs, and
    localhost / private addresses (literal IPs and hostnames with
    any private DNS answer). A hostname that does not
    resolve is allowed through — the analysis fetch reports that
    honestly.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("enter a website URL, e.g. example.com")
    url = _normalize_scheme(raw)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"{url!r} is not a website URL — try a domain like example.com")
    host = (parsed.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise ValueError("localhost is not a website to analyze")
    try:
        ipaddress.ip_address(host)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False
    if not is_literal_ip:
        # A domain name: letters, digits, hyphens and dots, with at
        # least one dot — so "not a url" and "example" are rejected
        # instead of sailing through to the fetch.
        if ("." not in host
                or not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?",
                                    host)
                or ".." in host):
            raise ValueError(
                f"{url!r} is not a website URL — try a domain like example.com")
    if is_literal_ip:
        if _is_private_address(host):
            raise ValueError("private addresses are not websites to analyze")
    else:
        resolved = _resolved_addresses(host)
        if any(_is_private_address(a) for a in resolved):
            raise ValueError(
                f"{host} resolves to a private address")
    return url


def validate_country(raw: object) -> str:
    """Market country: any real ISO 3166-1 alpha-2 code, lowercased;
    empty means "ca". Anything else is rejected."""
    return normalize_country(raw)


def validate_language(raw: object) -> str | None:
    """Optional market language (en, fr, en-ca …); empty means None."""
    lang = str(raw or "").strip().lower()
    if not lang:
        return None
    if not re.fullmatch(r"[a-z]{2,5}(-[a-z]{2})?", lang):
        raise ValueError("language must look like en, fr or en-ca")
    return lang


def new_job_store() -> dict:
    return {"jobs": {}, "lock": threading.Lock(), "preview_hits": {}}


def _default_runner(url: str, country: str, language: str | None,
                    write_report: bool = True) -> dict:
    """The CLI's pipeline, in-process: run the audits and return the
    on-page summary.

    With `write_report` (the default) the same report file the CLI
    writes is also saved, and its name is returned. Preview jobs
    pass False: no file is written and `report_name` is None — the
    full report comes only from the paid Analyze job.

    A site that cannot be fetched at all gets no scores: the auditors
    record a critical "availability" finding for that, and a number
    built from it would be a fake score. The panel shows
    "Unavailable — could not fetch" with the reason instead.
    """
    from ventoryn.reports import reports_dir
    from ecosystem.analyze import render_report, run_analysis, slug_for_url

    now = datetime.now(timezone.utc)
    result = run_analysis(url, country, language=language, now=now)
    text = render_report(result)
    if write_report:
        path = reports_dir() / f"analyze_{slug_for_url(url)}_{now:%Y%m%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        report_name = path.name
    else:
        report_name = None
    unreachable = any(
        f.get("severity") == "critical" and f.get("area") == "availability"
        for f in result["findings"]
    )
    return {
        "site_score": None if unreachable else result["site_score"],
        "geo_score": None if unreachable else result["geo_score"],
        "fetchable": not unreachable,
        "unavailable_reason": (
            "the site could not be fetched — check DNS, hosting and "
            "TLS for the domain."
            if unreachable else None
        ),
        "finding_counts": result["finding_counts"],
        "findings": [
            {"severity": f["severity"], "area": f["area"],
             "issue": f["issue"], "fix": f["fix"]}
            for f in result["findings"][:8]
        ],
        "report_name": report_name,
    }


def start_job(store: dict, url: str, country: str,
              language: str | None, runner=None,
              preview: bool = False) -> dict:
    """Create a job and run it in a background thread. Returns the
    job dict (status "queued"); the runner fills in result/error.

    `preview` marks a job started through the public /api/analyze
    endpoint: it runs the same pipeline but never writes the report
    file and never exposes a report name — the full report comes
    only from the paid Analyze job. The flag is on the job so the
    strip below holds even for a custom runner that returns one."""
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "url": url,
        "country": country,
        "language": language or "",
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
        "result": None,
        "report_name": None,
        "preview": bool(preview),
    }
    with store["lock"]:
        store["jobs"][job_id] = job
        while len(store["jobs"]) > _MAX_JOBS:
            oldest = min(store["jobs"],
                         key=lambda k: store["jobs"][k]["created_at"])
            del store["jobs"][oldest]
    thread = threading.Thread(
        target=_run_job, args=(store, job, runner), daemon=True)
    thread.start()
    return job


def _run_job(store: dict, job: dict, runner=None) -> None:
    with store["lock"]:
        job["status"] = "running"
    try:
        if job.get("preview") and runner is None:
            summary = _default_runner(
                job["url"], job["country"], job["language"] or None,
                write_report=False)
        else:
            summary = (runner or _default_runner)(
                job["url"], job["country"], job["language"] or None)
        if job.get("preview"):
            # Defense in depth: a preview job never exposes a report,
            # even if a custom runner returned one.
            summary = dict(summary)
            summary["report_name"] = None
        with store["lock"]:
            job["result"] = summary
            job["report_name"] = summary.get("report_name")
            job["status"] = "done"
    except Exception as exc:  # noqa: BLE001 - a failed job reports, never crashes
        with store["lock"]:
            job["status"] = "failed"
            job["error"] = str(exc) or exc.__class__.__name__


def get_job(store: dict, job_id: str) -> dict | None:
    """A copy of one job for the API, or None."""
    with store["lock"]:
        job = store["jobs"].get(job_id)
        return dict(job) if job is not None else None
