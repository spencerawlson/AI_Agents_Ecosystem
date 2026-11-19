# Merchant store connections — Shopify + WooCommerce

Axionory's merchant clients connect THEIR OWN stores so the agents
can read order and revenue data: return on ad spend, cost per
customer, conversion attribution (registry requirements 8 and 10).
Spencer holds no store; nothing here creates one, signs up, or
spends money. Read-only access only — no order creation, no
refunds, no product edits, no charges.

## The two connections

### Shopify — OAuth install for a custom app

A merchant installs Axionory's Shopify app on their store. The
flow is the standard authorization-code grant:

1. On the website page, the merchant types their store address
   (`your-store.myshopify.com`) and clicks **Connect with Shopify**.
2. Axionory redirects them to Shopify's authorize page with
   `client_id`, `scope`, `redirect_uri`, and a one-time `state`.
3. The merchant approves the install at Shopify; Shopify redirects
   back to `/account/auth/shopify/callback` with `code`, `shop`,
   `state`, `timestamp`, `host`, and `hmac`.
4. Axionory checks the `state` (single-use, 10-minute TTL), checks
   Shopify's HMAC over every returned parameter, exchanges the code
   for an **offline access token** (no expiry, no refresh token —
   it stops working only when the merchant uninstalls the app or
   the scopes change), then runs a read-only probe (shop identity +
   granted scopes) before marking the connection connected.

**Scopes — read-only, exactly these three:**

| Scope | Why |
|---|---|
| `read_orders` | Order and revenue data — the whole point (ROAS, attribution) |
| `read_products` | Attribute revenue to products |
| `read_customers` | Customer counts for cost per customer |

No write scope is ever requested. The token is stored encrypted
per tenant (see below) and revoked at Shopify on disconnect.

**Shop domain validation:** the shop host is merchant-supplied, so
it is validated against an anchored `*.myshopify.com` pattern
before any URL is built from it — a forged shop must never receive
the token exchange, which carries the client secret.

**Callback HMAC:** the callback is a public endpoint, so its HMAC
is verified before anything else happens with its parameters.
Shopify's rule: drop `hmac`, escape `%` → `%25`, `&` → `%26`,
`=` → `%3D` in keys and values, sort, join `k=v` with `&`,
HMAC-SHA256 with the client secret, constant-time compare.

**Operator steps (one-time, before any merchant can connect):**

1. In the Shopify Partner Dashboard, create the app (custom
   distribution) and set its redirect URI verbatim to
   `{base}/account/auth/shopify/callback` — e.g.
   `https://www.axionory.com/account/auth/shopify/callback`.
2. Set in `.env.ventoryn` (server file, never in chat):
   `VENTORYN_SHOPIFY_CLIENT_ID` and
   `VENTORYN_SHOPIFY_CLIENT_SECRET`.
3. Restart the Ventoryn container.

Until then the Shopify row honestly says the app is not set up yet
and the start route refuses with 400 — nothing is faked.

### WooCommerce — API key

The merchant generates a **Read** key in their own WordPress admin
(WooCommerce → Settings → Advanced → REST API) and pastes the
store URL + consumer key + consumer secret into the form on the
website page. Axionory validates the URL (**HTTPS only** — the
secret must never travel over plain HTTP; plain HTTP is refused,
not downgraded), then runs a read-only probe
(`GET /wp-json/wc/v3/orders?per_page=1`, HTTP Basic Auth with the
key as username and the secret as password). The key is stored
encrypted only when the probe passes — a failed test stores
nothing. No operator setup is needed; no app review; the store
owner holds the keys. A Write key is never asked for.

## Storage, encryption, rotation

- Tokens and keys live in `saas_connections.detail`, encrypted with
  the same per-value scheme as every other stored token
  (`ventoryn/token_crypto.py`, key `VENTORYN_TOKEN_KEY`):
  Shopify `access_token`; WooCommerce `consumer_key` +
  `consumer_secret`. Identity fields are not secrets and are shown
  in the UI: Shopify `shop_domain`, `shop_name`, `currency_code`,
  `scope`; WooCommerce `store_url`.
- `get_connections` strips every secret from every row; the site
  page never renders a key, token, or secret.
- Tenant isolation (requirement 20): each site's connection rows
  belong to that site's account; every route checks the site's
  `account_id` (wrong-account → 404).
- Rotation (requirement 17): Shopify tokens rotate by re-running
  the install (the old token is revoked after the new one works);
  WooCommerce keys rotate in wp-admin and are re-entered here.
  Rotation is per tenant; record the date + reference in the
  registry execution log, never the value.
- Disconnect requires a fresh TOTP code, revokes the Shopify token
  at Shopify (DELETE
  `/admin/api/{version}/api-permissions/current.json` with the
  token header; a failed revoke is reported, the local copy is
  still deleted, and the merchant is told to remove the app in
  Shopify admin), and wipes the stored detail. WooCommerce has no
  revoke API — the merchant deletes the key in wp-admin.

## Code map

- `ventoryn/commerce_oauth.py` — the provider adapters (mirrors
  `ventoryn/social_oauth.py`): injected HTTP client, constant
  URLs only, `SaasError` on failure, no live calls at import time.
  Shopify: `validate_shop_domain`, `build_authorize_url`,
  `validate_callback_hmac`, `exchange_code`,
  `verify_shopify_connection`, `revoke_shopify_token`. WooCommerce:
  `validate_store_url`, `test_woocommerce_connection`.
- `ventoryn/saas.py` — `SaasConfig.shopify_client_id/secret`,
  `shopify_configured`, `app_registered("shopify")`;
  `DEFAULT_CONNECTIONS` gains `shopify` + `woocommerce`
  (backfilled onto older sites by `ensure_connections`);
  `CONNECTION_SCOPES` / `CONNECTION_CONSENT` /
  `CONNECTION_REVOKE_NOTE` entries for both;
  `_SECRET_DETAIL_KEYS` gains `consumer_key`/`consumer_secret`;
  `revoke_provider_token` handles both kinds.
- `ventoryn/entitlements.py` — `shopify` + `woocommerce` join
  `UTILITY_INTEGRATIONS`: read-only data connections, open to every
  plan, like Plausible / Stripe sales. They do not publish, so they
  are not premium integrations.
- `ventoryn/saas_routes.py` — `POST
  /websites/sites/{site_id}/connections/shopify/start` (form shop
  domain → validate → state → 303 to Shopify authorize);
  `GET /account/auth/shopify/callback` (state + HMAC + exchange +
  verify → connected); the generic `POST
  /websites/sites/{site_id}/connections/{kind}` gains the
  `woocommerce` branch (validate + probe + store on success only)
  and refuses `shopify` through the Connect link; the site-detail
  connections loop renders both rows (connected: store identity;
  not connected: the connect form, or the honest not-set-up note
  for Shopify).
- `ventoryn/app.py` — the /integrations grid gains both apps
  (Shopify, WooCommerce) with read-only blurbs, and the per-app
  window builder renders the same connect forms (Shopify domain
  form, WooCommerce key form) with the same endpoints and the same
  honest not-set-up note.
- `core/shopify_adapter.py` — unchanged. The connection probe uses
  the adapter's `graphql()` with its own scopes query, because the
  adapter's `verify()` checks the owner-operated store pipeline's
  scope list, which omits `read_customers`.

## Tests

- `tests/test_commerce_oauth.py` — authorize URL + read-only scopes,
  domain validation (valid + forged hosts), HMAC (valid / tampered /
  missing), code exchange (mocked: success, invalid grant, no token,
  unconfigured app), verify (mocked transport: missing-scope report,
  full scopes, 401), revoke (mocked: 200 / 500 / empty token),
  WooCommerce URL validation (https / http / userinfo / no scheme),
  connection test (mocked: 200 list / 401 / non-JSON / http URL /
  missing key), config + registration, encrypted storage round-trip
  (raw rows hold ciphertext only; `get_connections` strips secrets),
  disconnect wiping detail, scope/consent strings read-only.
- `tests/test_commerce_routes.py` — through the real routes:
  site page renders both rows (and the honest not-set-up note);
  Shopify start (unconfigured 400, configured 303 to Shopify,
  bad domain 400, wrong account 404); callback (mocked exchange +
  verify → connected with shop identity; bad state 400; bad HMAC
  400; failed exchange stores nothing); WooCommerce connect (mocked
  test → connected, store shown, keys never rendered; http URL 400
  + nothing stored; failed test 400 + nothing stored); disconnect
  (mocked revoke → detail wiped for both kinds); /integrations
  grid lists both apps and the per-app windows render the connect
  forms (or the not-set-up note for unconfigured Shopify).
- `tests/test_saas_shell.py` — `test_site_defaults_to_honest_connections`
  updated: the exact-kind set gains `shopify` + `woocommerce`, with
  honesty assertions for both (Shopify's default note names the app
  registration; both consent texts are read-only).

## Follow-ups (not this PR)

- Shopify + WooCommerce webhook verification (HMAC) for order
  events, and an orders sync job feeding requirement 10 (revenue
  verification) and the report/attribution views.
- A live-browser re-check of the Shopify endpoints and HMAC rule
  against shopify.dev before the first real merchant connects
  (the endpoints here come from the registry + the documented
  authorization-code grant, verified against secondary sources).
- One third-party note, recorded not blocking: Shopify's 2025–2026
  policy reportedly discourages apps asking merchants to type
  their myshopify.com URL (anti-phishing; install links preferred).
  This flow is the custom-app install where the merchant enters
  their own store domain; revisit if Shopify's review objects.
