"""Alerts: what needs the owner's attention, in one place.

Alerts are not shown inline on the pages any more (they cluttered
Integrations). They live on /alerts and in the notification bell's
panel, with a count on the bell.

Each alert is a dict: ``level`` ("warn" or "info"), ``text`` (plain
text, escaped when rendered), ``href`` (where to fix it) and ``action``
(the link label).
"""

from __future__ import annotations

import html


def _esc(v) -> str:
    return html.escape(str(v), quote=True)


def account_alerts(store, account: dict, pending_approvals: int = 0) -> list[dict]:
    alerts: list[dict] = []
    sites = store.list_sites(account["id"])
    # Work waiting for a decision.
    proposals = store.list_campaigns(account["id"], status="proposed")
    if proposals:
        n = len(proposals)
        alerts.append({"level": "info", "href": "/approvals",
                       "action": "Review",
                       "text": f"{n} campaign proposal{'s' if n != 1 else ''} waiting for your approval."})
    if pending_approvals:
        alerts.append({"level": "info", "href": "/approvals", "action": "Review",
                       "text": f"{pending_approvals} approval{'s' if pending_approvals != 1 else ''} waiting."})
    # Approved campaign posts that could not go out.
    for c in store.list_campaigns(account["id"], status="scheduled", limit=20):
        held = [d for d in store.campaign_drafts(c["id"]) if d["status"] == "held"]
        if held:
            site = store.get_site(c["site_id"]) or {}
            alerts.append({"level": "warn", "href": f"/approvals#campaign-{c['id']}",
                           "action": "See campaign",
                           "text": f"{len(held)} X post{'s' if len(held) != 1 else ''} for "
                                   f"{site.get('host', '')} could not be posted: {held[0]['note']}"})
    # Websites whose sales cannot be verified.
    for s in sites:
        conn = store.get_connection(s["id"], "stripe_sales") or {}
        if conn.get("status") != "connected":
            alerts.append({"level": "warn",
                           "href": f"/integrations/stripe_sales?site={s['id']}",
                           "action": "Connect Stripe",
                           "text": f"Connect Stripe sales for {s['host']} to count paying "
                                   f"customers — the funnel cannot verify sales without it."})
    return alerts


def alert_rows_html(alerts: list[dict], limit: int | None = None) -> str:
    items = alerts[:limit] if limit else alerts
    return "".join(
        f"<li class='v-alert v-alert-{_esc(a['level'])}'><span>{_esc(a['text'])}</span>"
        f"<a href='{_esc(a['href'])}'>{_esc(a['action'])}</a></li>" for a in items)


#: The alert styles live in the platform shell (ventoryn/nav.py) so the
#: bell panel and /alerts share them.
ALERTS_CSS = ""
