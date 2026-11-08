"""Background site-analysis jobs for the Collegium dashboard.

The /ops page's "Analyze a site" panel starts a job here; the same
``ecosystem.analyze`` pipeline the CLI runs does the work in a
background thread, and the page polls for the result. Nothing is
published or posted anywhere: the only output is the local Markdown
report (the same file the CLI writes) plus the on-page summary.

Jobs live on the app instance (``app.state.analyze_jobs``), so tests
get a fresh store per app and a restart drops in-flight jobs — the
page treats a 404 on poll as "the dashboard restarted, run it again".
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

_MAX_JOBS = 20


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
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    # 198.18.0.0/15 is IANA benchmarking space, never a LAN — but the
    # sandbox egress proxy resolves every hostname into it, so it must
    # not count as "private" here. A hostname resolving there is either
    # that proxy (and the fetch reports honestly) or a non-routable
    # answer (and the fetch fails honestly); neither is an internal
    # service.
    if ip in ipaddress.ip_network("198.18.0.0/15"):
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def validate_url(raw: object) -> str:
    """Normalize a website URL, or raise ValueError with the reason.

    http/https only; rejects empty input, non-URLs, and localhost /
    private addresses (literal IPs and hostnames that resolve only
    to private addresses). A hostname that does not resolve is
    allowed through — the analysis fetch reports that honestly.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("enter a website URL, e.g. https://example.com")
    url = raw.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"{url!r} is not a website URL — use https://example.com")
    host = (parsed.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise ValueError("localhost is not a website to analyze")
    try:
        ipaddress.ip_address(host)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False
    if is_literal_ip:
        if _is_private_address(host):
            raise ValueError("private addresses are not websites to analyze")
    else:
        resolved = _resolved_addresses(host)
        if resolved and all(_is_private_address(a) for a in resolved):
            raise ValueError(
                f"{host} resolves only to private addresses")
    return url


def validate_country(raw: object) -> str:
    """2-letter market country code, lowercased; empty means "ca"."""
    code = str(raw or "").strip().lower() or "ca"
    if not re.fullmatch(r"[a-z]{2}", code):
        raise ValueError("country must be a 2-letter code (e.g. ca)")
    return code


def validate_language(raw: object) -> str | None:
    """Optional market language (en, fr, en-ca …); empty means None."""
    lang = str(raw or "").strip().lower()
    if not lang:
        return None
    if not re.fullmatch(r"[a-z]{2,5}(-[a-z]{2})?", lang):
        raise ValueError("language must look like en, fr or en-ca")
    return lang


def new_job_store() -> dict:
    return {"jobs": {}, "lock": threading.Lock()}


def _default_runner(url: str, country: str, language: str | None) -> dict:
    """The CLI's pipeline, in-process: run the audits, write the
    same report file the CLI writes, return the on-page summary.

    A site that cannot be fetched at all gets no scores: the auditors
    record a critical "availability" finding for that, and a number
    built from it would be a fake score. The panel shows
    "Unavailable — could not fetch" with the reason instead.
    """
    from dashboard.reports import reports_dir
    from ecosystem.analyze import render_report, run_analysis, slug_for_url

    now = datetime.now(timezone.utc)
    result = run_analysis(url, country, language=language, now=now)
    text = render_report(result)
    path = reports_dir() / f"analyze_{slug_for_url(url)}_{now:%Y%m%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
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
        "report_name": path.name,
    }


def start_job(store: dict, url: str, country: str,
              language: str | None, runner=None) -> dict:
    """Create a job and run it in a background thread. Returns the
    job dict (status "queued"); the runner fills in result/error."""
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
        summary = (runner or _default_runner)(
            job["url"], job["country"], job["language"] or None)
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
