"""App-store listing audit program.

Audits Spencer's iOS apps against the public App Store record —
Apple's iTunes Search API, no key, no sign-in (see
``core/app_store_audit.py``). One audit per app:

    python3 ecosystem/app_store_audit.py audit --app kayayiti
    python3 ecosystem/app_store_audit.py audit --app farelune
    python3 ecosystem/app_store_audit.py audit --app-id 6826322327 --country ca
    python3 ecosystem/app_store_audit.py --list

An app that is not on the store yet ("Ready for Review", "Prepare
for Submission") is reported as not listed, with the readiness
checklist to verify in App Store Connect — never scored as if a
listing existed. Reports land in reports/app_store_<app>_<date>.md.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.app_store_audit import audit_app  # noqa: E402
from dashboard.reports import reports_dir  # noqa: E402

# Spencer's apps, by their App Store Connect Apple ID. The IDs are
# public store identifiers, not secrets.
APPS = {
    "kayayiti": {
        "app_id": "6826322327",
        "country": "ca",
        "note": "Kay Ayiti — Haitian rental listings. French-only listing.",
    },
    "farelune": {
        "app_id": "6828243842",
        "country": "us",
        "note": "Farelune — flight comparison. ASC record exists; "
                "not submitted.",
    },
}


def _write_report(app: str, result: dict, reports_root: Path,
                  now: datetime) -> Path:
    lines = [f"# App-store audit — {app} ({now:%Y-%m-%d})", ""]
    if not result["listed"]:
        lines += [
            "Status: **not on the App Store** in the storefronts checked "
            f"({', '.join(result['storefronts_checked'])}).", "",
            "## Findings", "",
        ]
    else:
        details = result["details"]
        lines += [
            f"Listing score: **{result['score']}/100** — "
            f"{details['name']} v{details['version']} "
            f"({details['ratings']} ratings, "
            f"avg {details['average_rating']}, "
            f"{details['screenshots']} screenshots, "
            f"{details['description_chars']} description chars, "
            f"languages: {', '.join(details['languages'])}).", "",
            "## Findings", "",
        ]
    for f in result["findings"]:
        lines.append(f"- **[{f['severity']}] {f['area']}** — {f['issue']} "
                     f"— Fix: {f['fix']}")
    if not result["findings"]:
        lines.append("- None. The listing is in good shape.")
    lines += ["", result["note"]]
    path = reports_root / f"app_store_{app}_{now:%Y%m%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit an iOS app's App Store listing (public record).")
    parser.add_argument("--list", action="store_true",
                        help="list the configured apps")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("audit", help="audit one app's listing")
    p.add_argument("--app", choices=sorted(APPS),
                   help="configured app to audit")
    p.add_argument("--app-id", help="Apple ID (overrides --app)")
    p.add_argument("--country", default=None,
                   help="storefront country code (default: the app's)")
    args = parser.parse_args(argv)
    if args.list:
        for name, cfg in sorted(APPS.items()):
            print(f"{name}: Apple ID {cfg['app_id']} "
                  f"({cfg['country']}) — {cfg['note']}")
        return 0
    if args.cmd != "audit":
        parser.print_help()
        return 2
    if args.app_id:
        name, app_id = args.app_id, args.app_id
        country = args.country or "us"
    elif args.app:
        cfg = APPS[args.app]
        name, app_id = args.app, cfg["app_id"]
        country = args.country or cfg["country"]
    else:
        parser.error("audit needs --app or --app-id")
        return 2
    now = datetime.now(timezone.utc)
    try:
        result = audit_app(app_id, country=country, now=now)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    if not result["listed"]:
        print(f"{name}: not on the App Store "
              f"({', '.join(result['storefronts_checked'])})")
    else:
        d = result["details"]
        print(f"{name}: score {result['score']}/100 — {d['name']} "
              f"v{d['version']}, {d['ratings']} ratings, "
              f"{d['screenshots']} screenshots")
    for f in result["findings"]:
        print(f"  [{f['severity']}] {f['area']}: {f['issue']}")
    path = _write_report(name, result, reports_dir(), now)
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
