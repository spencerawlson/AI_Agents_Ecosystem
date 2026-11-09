# Ventoryn Analytics

First-party, cookieless analytics for customer sites — no Plausible
account needed. Code: `ventoryn/analytics.py`; tests:
`tests/test_ventoryn_analytics.py`.

## Install (customer)

On the website's page in Ventoryn, copy the snippet into `<head>` on
every page:

```html
<script defer data-site="<site id>" src="https://ventoryn.app/v.js"></script>
```

Signups: call `ventoryn('signup')` when someone signs up (any event name
matching `[a-z0-9_-]{1,40}` works). **Check my installation** fetches the
home page and confirms the snippet is there with the right site id. A
site that sends a Content-Security-Policy must allow the Ventoryn origin
in `script-src` and `connect-src`.

## What it records

Page views (including single-page-app navigation), the entry source per
visitor per day (`utm_source`, else the referring host, else
"Direct / None"; same-site clicks never replace it), custom events, and
visits that arrived through a **signed Ventoryn link** (the `vtn_` tag in
`utm_content`, counted only if Ventoryn issued that tag for that site).

Where it shows up: the site's funnel (visitors, signups), the Analytics
page, and the attribution flow's **Website Visit** step, which is checked
when a signed-link visit was recorded. Plausible stays as the fallback
for sites that have it connected and no snippet.

## Privacy

- No cookies or storage on the visitor's device.
- Visitor = SHA-256 of (daily random salt, site, IP, user agent). Salts
  rotate every UTC day and old ones are deleted, so a person cannot be
  linked across days and the hash cannot be reversed.
- Raw events live only for the current day; completed days are rolled
  up into daily totals and the raw rows (hashes included) are deleted.
- IPs and user agents are never stored.
- Visitors are counted per day, like Plausible: three visits on three
  days count as three.

## Abuse protection (`POST /api/va/collect`)

Public by design and exempt from the cross-site request check, so it has
its own: the page URL and any `Origin` must be on the site's host (or a
subdomain), bots/automation are dropped, bodies over 2 KB get 413, 120
events per minute per client IP (429), and every other rejection is a
silent 204 so probes learn nothing about which site ids exist.

## Scale notes

Storage is the SaaS SQLite database (`va_*` tables). Fine for early
traffic; move to Postgres as volume grows. Ad blockers can hide some
visitors from any third-party tracker; serving the script from the
customer's own domain is the later fix.
