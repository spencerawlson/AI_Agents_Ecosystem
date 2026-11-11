"""Alerts: what needs the owner's attention, in one place.

Alerts are not shown inline on the pages (they cluttered Integrations).
They live on /alerts and in the notification bell's panel; the bell's
badge counts the UNREAD ones.

Each alert is a dict: ``key`` (stable id), ``level`` ("warn" or
"info"), ``text`` (plain text, escaped when rendered), ``href`` (where
to fix it), ``action`` (the link label) and ``read``.

Read state: opening an alert (its link goes through /alerts/open) or
pressing "Mark as read" stores its key for the account. A key carries
the alert's state — the number of proposals waiting, of posts held —
so when that changes the alert is unread again; a fixed problem simply
disappears.
"""

from __future__ import annotations

import html
import urllib.parse


def _esc(v) -> str:
    return html.escape(str(v), quote=True)


def account_alerts(store, account: dict, pending_approvals: int = 0) -> list[dict]:
    alerts: list[dict] = []
    sites = store.list_sites(account["id"])
    # Work waiting for a decision.
    proposals = store.list_campaigns(account["id"], status="proposed")
    if proposals:
        n = len(proposals)
        alerts.append({"key": f"proposals:{n}:{proposals[0]['id']}", "level": "info",
                       "href": "/approvals", "action": "Review",
                       "text": f"{n} campaign proposal{'s' if n != 1 else ''} waiting for your approval."})
    if pending_approvals:
        alerts.append({"key": f"approvals:{pending_approvals}", "level": "info",
                       "href": "/approvals", "action": "Review",
                       "text": f"{pending_approvals} approval{'s' if pending_approvals != 1 else ''} waiting."})
    # Approved campaign posts that could not go out.
    for c in store.list_campaigns(account["id"], status="scheduled", limit=20):
        held = [d for d in store.campaign_drafts(c["id"]) if d["status"] == "held"]
        if held:
            site = store.get_site(c["site_id"]) or {}
            alerts.append({"key": f"held:{c['id']}:{len(held)}", "level": "warn",
                           "href": f"/approvals#campaign-{c['id']}", "action": "See campaign",
                           "text": f"{len(held)} X post{'s' if len(held) != 1 else ''} for "
                                   f"{site.get('host', '')} could not be posted: {held[0]['note']}"})
    # Websites whose sales cannot be verified.
    for s in sites:
        conn = store.get_connection(s["id"], "stripe_sales") or {}
        if conn.get("status") != "connected":
            alerts.append({"key": f"stripe:{s['id']}", "level": "warn",
                           "href": f"/integrations/stripe_sales?site={s['id']}",
                           "action": "Connect Stripe",
                           "text": f"Connect Stripe sales for {s['host']} to count paying "
                                   f"customers — the funnel cannot verify sales without it."})
    read = store.read_alert_keys(account["id"])
    for a in alerts:
        a["read"] = a["key"] in read
    return alerts


def unread(alerts: list[dict]) -> list[dict]:
    return [a for a in alerts if not a.get("read")]


def alert_rows_html(alerts: list[dict], limit: int | None = None) -> str:
    """Rows: the text, the action link (opening marks it read) and a
    Mark as read button for unread ones."""
    items = alerts[:limit] if limit else alerts
    out = ""
    for a in items:
        q = urllib.parse.quote(a["key"], safe="")
        mark = ("" if a.get("read") else
                f"<form method='post' action='/alerts/read' class='v-alert-read'>"
                f"<input type='hidden' name='key' value='{_esc(a['key'])}'>"
                f"<button type='submit' class='v-alert-mark'>Mark as read</button></form>")
        out += (f"<li class='v-alert v-alert-{_esc(a['level'])}{' is-read' if a.get('read') else ''}'>"
                f"<span>{_esc(a['text'])}</span><span class='v-alert-actions'>"
                f"<a href='/alerts/open?key={q}'>{_esc(a['action'])}</a>{mark}</span></li>")
    return out


MARK_ALL_FORM = ("<form method='post' action='/alerts/read-all' class='v-alert-all'>"
                 "<button type='submit' class='v-alert-mark'>Mark all as read</button></form>")

#: Mark as read in place: the row dims, the badge goes down by one.
ALERTS_JS = (
    "<script>document.addEventListener('submit',function(e){var f=e.target;"
    "if(!f.matches||!(f.matches('form.v-alert-read')||f.matches('form.v-alert-all')))return;"
    "e.preventDefault();fetch(f.action,{method:'POST',body:new FormData(f),credentials:'same-origin',"
    "headers:{'Accept':'application/json'}}).then(function(r){if(!r.ok)throw r;return r.json();})"
    ".then(function(d){var all=f.matches('form.v-alert-all');"
    "var rows=all?document.querySelectorAll('.v-alert:not(.is-read)'):[f.closest('.v-alert')];"
    "rows.forEach(function(row){if(!row)return;row.classList.add('is-read');"
    "var k=row.querySelector('form.v-alert-read');if(k)k.remove();});"
    "if(all){document.querySelectorAll('form.v-alert-read').forEach(function(x){"
    "var r=x.closest('.v-alert');if(r)r.classList.add('is-read');x.remove();});}"
    "var n=d.unread,b=document.getElementById('bell-btn');"
    "if(b){b.setAttribute('aria-label','Alerts ('+n+')');var g=b.querySelector('.header-badge');"
    "if(n>0){if(!g){g=document.createElement('b');g.className='header-badge';b.appendChild(g);}g.textContent=n;}"
    "else if(g){g.remove();}}}).catch(function(){f.submit();});});</script>")

#: The alert styles live in the platform shell (ventoryn/nav.py) so the
#: bell panel and /alerts share them.
ALERTS_CSS = ""
