# Ventoryn SaaS shell

The piece that turns the internal Ventoryn dashboard into a product a
website owner can buy: sign in with Google, subscribe for **$20/month**,
add a website, and Ventoryn works to bring that site its first 100
paying customers. The proof case is Road to CISSP (road2cissp.com).

## What the customer gets

- **The landing page** (`/`) — deep navy / near-black hero with
  electric violet + blue gradients, sticky public nav (Product / How
  It Works / Attribution / Pricing / FAQ + Sign In / Get Started),
  the URL box with a free preview (site score, GEO score, top
  findings), an SVG dashboard preview (illustrative demo data, clearly
  labeled), the 5-step How It Works, six feature sections, the
  attribution explainer, differentiation, who-it's-for, pricing from
  the verified Stripe data ($20/month, 100 credits), 10 FAQs, and a
  dark final CTA. No sign-in needed to read it; it shows no internal
  data and links to no operator page. Every graphic is vector (inline
  SVG); there is no og-image (a bitmap would break the vector-only
  rule — Spencer's call whether to add one).
- **Sign-in with Google, on the landing page** — no passwords, no
  separate bare sign-in page (`/account/login` redirects to `/`).
  After sign-in EVERYONE goes straight to **Live Ops** (`/ops`) —
  the post-sign-in home for every account, scoped to that account.
  **Two-factor is mandatory from the first sign-in** (see below):
  a new account is taken to the 2FA setup page and cannot reach Live
  Ops, the portfolio, or /admin until setup completes.
- **Their own Live Ops** — the crew, activity feed and stats show
  only that account's sites and their work. No sites yet means an
  empty state with the URL box to add one. The customer nav is:
  Live Ops, My Portfolio, Approvals, Report, My account, Subscribe,
  Sign out.
- **Their own portfolio, report and approvals** — `/portfolio` is
  their sites (colored stat + business cards, in the Live Ops visual
  language); `/report` is their Acquisition & Attribution report —
  progress toward 100 verified paying customers, Verified / Pending /
  Unattributed counts, the attribution flow with evidence checks, the
  conversion-status donut, campaign performance, the acquisition
  trend from real data, and the Data & Privacy controls (see below);
  `/approvals` is their approvals, WITH an approve button. Nothing
  ships until that customer approves it — they pay for it, they
  decide. The operator's own report (Agent & Financial Report) is a
  different page and keeps the old shell untouched.
- **$20/month plan** — "Ventoryn Growth". (`/account/subscribe`)
- **Credits** — agent work costs credits; the plan includes a monthly
  allowance (see the cost table below). Jobs that cost more than the
  balance are refused with the balance shown; balances never go negative.
- **Website onboarding** — add a site (market country from a full
  ISO 3166-1 selector, 249 countries, default ca), connect Plausible
  and the site's own Stripe webhook, see the funnel visitors →
  signups → paying customers. Every number comes from a real source;
  where a connection is missing the value reads "unavailable", never
  invented.
- **Connections** — per site: Reddit, X, Plausible (analytics),
  Stripe sales. Each shows its status (connected / not connected)
  with Connect and Disconnect. Agents may only post/reply on accounts
  that are actually connected, and only after the customer's approval
  (drafts, the 90/10 rule). Honest limit, stated on the page:
  Reddit/X posting needs the platform OAuth app registrations first
  (the operator registers the apps in the Reddit/X developer
  consoles — Spencer's step); until then the connection says
  "not connected — app registration needed" and Connect explains what
  is missing. Nothing is faked. The Connect screen for each provider
  states plainly, in plain words, what Ventoryn can do as the
  customer (post/reply as them; read payments) and what it cannot do
  (no DMs, no charges, no refunds, no account settings). Minimum
  scopes: Reddit submit/read/identity; X post/read only, never DMs;
  Stripe read payments only, never charge/refund. X's API is paid and
  rate-limited — the X connection page says so plainly. Tokens are
  stored ENCRYPTED at rest (see below), server-side only, never sent
  to the browser. Disconnect REVOKES the token at the provider, then
  deletes it locally. A per-account "pause all posting" switch stops
  every post/reply.
- **Revenue plug-in + display choice** — the customer plugs their
  site's Stripe sales in (Stripe sales connection) so Ventoryn sees
  every payment. Data & Privacy controls on the site page are THREE
  separate settings: Show revenue (dollar amounts — OFF by default),
  Show customer count (ON by default), Show conversion details (ON by
  default). Revenue appears in the report and funnel only when the
  revenue toggle is ON and Stripe sales is connected; off (or
  disconnected) hides every dollar figure everywhere, including the
  admin totals — the sale still counts as a paying customer toward
  the 100 goal. Revenue figures are per-account scoped and never go
  into logs.
- **Attribution — the Ventoryn signature** — every agent output that
  contains a link carries a signature: UTM parameters
  (`utm_source=ventoryn`, `utm_medium=<agent>`,
  `utm_campaign=<site>`) PLUS a signed per-action tag (e.g.
  `vtn_<id>`) generated per site + per agent action and RECORDED when
  the link is created (what it points to, which agent, which job,
  when). A payment is attributed to Ventoryn work ONLY where the
  signature reaches the payment (the tag in the Stripe metadata /
  webhook match); otherwise the sale is reported as unattributed,
  never assigned by guesswork. The signature is shown next to each
  attributed sale in the report. The site page tells the customer
  plainly that the checkout must carry the tag for attribution to
  prove out.
- **Two-factor, mandatory** — TOTP two-factor is required for every
  account from the first sign-in, customer and admin alike. Flow:
  Google sign-in (or the admin password) → if 2FA is not set up, the
  setup page (QR code + manual key shown once, confirm by entering a
  code, backup codes shown once) and the session is limited to that
  page until setup completes; once set up, the code is required at
  every sign-in. Backup codes work once each and then fail. The setup
  page explains in plain words why it is required and what to do if
  the phone is lost (backup codes; the operator can reset 2FA for an
  account from /admin — an audited admin action that forces setup
  again at the next sign-in). The TOTP secret is stored encrypted;
  backup codes are stored hashed. TOTP is implemented with the
  standard library only (hmac/hashlib/base64) and shared between the
  admin and customer flows — one implementation, not two.
- **Zero-trust** — every request is validated server-side (token →
  account lookup in the store, expiry enforced, revoked sessions
  rejected); nothing is trusted from the network, IP, or headers. A
  session deleted server-side stops working immediately, even if the
  cookie is still in the browser. Every route enforces account
  scoping. The admin session is SEPARATE from the Google customer
  session — a signed-in Google session, even the operator's, never
  opens /admin. Sensitive actions (disconnecting a connection,
  changing a revenue-display toggle, disconnecting Stripe) require a
  fresh 2FA code when the account has 2FA enabled; if 2FA is not
  enabled, the action proceeds on the verified session and the page
  recommends enabling 2FA.
- **Jobs** — "Analyze my site" runs today (5 credits). Other job types
  are priced in the table and marked "coming soon" until they are wired
  to the agent machinery.

Nothing auto-publishes or posts. No agent refunds or charges. Approvals
stay human.

## The customer/admin split (ShipItAyiti flow)

The landing page → sign in / sign up (Google, on the landing page)
→ everyone goes straight to Live Ops (`/ops`). `/ops` requires
sign-in and is scoped to the signed-in account — never another
customer's sites, never global revenue. The operator (Spencer) signs
up like any customer; his existing ecosystem businesses (Farelune,
Road to CISSP, the Notion templates) are attached to his account by
the operator email in the environment (see below) — the mechanism
takes the email from env config, it is never hardcoded.

**Admin is a separate URL: `ventoryn.app/admin`.** It shows ONLY:
who is live on the platform now, who subscribed, all the accounts
on the platform, and what the agents are doing (activity, health,
problems). It does NOT show any customer's portfolio or approvals —
those are the customer's own financial decisions, made alone. No
approve-on-behalf, no per-customer site URLs or funnels in `/admin`.
Admin pages: Live, Subscribers, Accounts, Agents.

**The admin needs a password, not a Google session.**
`VENTORYN_ADMIN_PASSWORD` in the server environment (Spencer sets
the value himself — never in chat, email, or the repo). Typing
`/admin` without the password session goes to `/admin/login`, a
password-only page (no username, no email, no Google button); a
successful login sets an HttpOnly session cookie (24h, sliding).
If the env var is unset, login always fails with the same generic
message — fail closed. A signed-in Google session alone never opens
`/admin`, not even the operator's. The operator email
(`SAAS_OPERATOR_EMAIL`) still attaches Spencer's businesses to his
customer account; it just no longer opens `/admin` by itself.

**Admin two-factor.** The admin login also supports TOTP
two-factor (password + 6-digit authenticator code), set up under
`/admin` after the password login: the secret is generated there,
shown ONCE as a QR code + manual key (the operator scans it in his
own session — the secret never goes in chat or the repo), confirmed
by entering a code before 2FA is active, with one-time backup codes
(shown once, stored hashed). The TOTP secret is stored encrypted.
Until 2FA is enabled, password-only login still works (so the build
itself can never lock him out); once enabled, the code is required
at every login — a password-only session no longer opens /admin.
The password is compared server-side in constant time; wrong guesses
are rate-limited (lockout after 5 failures for 15 minutes); every
admin login is recorded in the audit log. The admin can reset a
customer's 2FA from /admin/accounts (audited; forces setup again at
the customer's next sign-in) and reset his own from the 2FA setup
page.

**Live now** is honest presence, not fake real-time: a signed-in
account's last-seen timestamp is stamped on its requests (stored in
the shared state store) and `/admin` lists the accounts active
within the last 5 minutes with their last action; stale sessions
drop off. The recent-activity feed lists only real events:
sign-ins, websites added, subscriptions, analyze runs.

Neither nav links Experiments, Experiment 001, Daily plan,
Attribution, or "for website owners" — those stay reachable by direct
URL only (operator tools, operator account only). `/api/snapshot`
and `/api/report` require a signed-in session and are scoped the same
way; an unsigned caller gets 401. (DJINN's read-only integration
reads those two endpoints, so it needs an operator session.)

## Credit costs (starting table — Spencer can change it)

| Agent work | Cost | Status |
| --- | --- | --- |
| Analyze a site (site + GEO audit) | 5 credits | Available now |
| GEO audit | 5 credits | Coming soon |
| Community listening run | 5 credits | Coming soon |
| Social analytics report | 5 credits | Coming soon |
| SEO article draft | 10 credits | Coming soon |
| Video render (script → MP4 + SRT) | 15 credits | Coming soon |

Monthly allowance for the $20 plan: **100 credits** by default,
configurable with `SAAS_MONTHLY_CREDITS`. The allowance is granted when
a subscription starts and again on each renewal invoice.

## Setup — Spencer's steps

### 1. Google sign-in (Google Cloud Console)

1. Go to [Google Cloud Console](https://console.cloud.google.com) →
   **APIs & Services → Credentials**.
2. If there is no OAuth consent screen yet: **OAuth consent screen** →
   **External** → app name `Ventoryn`, support email your own →
   scopes `openid`, `email`, `profile` (the defaults) → save. While
   the app is in **Testing**, only the test users you list can sign in;
   publish it when you are ready for strangers.
3. **Create Credentials → OAuth client ID → Web application**, name
   `Ventoryn`.
4. Under **Authorized redirect URIs** add exactly:
   `https://ventoryn.app/account/auth/google/callback`
   (for local testing: `http://localhost:8000/account/auth/google/callback`).
   The redirect URI must match character for character — scheme, host,
   port, and path.
5. Copy the **Client ID** → `GOOGLE_CLIENT_ID` and the **Client secret**
   → `GOOGLE_CLIENT_SECRET` in the server environment. Never paste
   either into chat, email, or the repo.

### 2. Stripe (Stripe Dashboard)

1. **Products → Add product**: name `Ventoryn Growth`, recurring price
   **$20.00 / month (USD)** → copy the **Price ID** → `STRIPE_PRICE_ID`.
2. **Developers → API keys**: copy the **Secret key** →
   `STRIPE_SECRET_KEY` (use a test key while testing, the live key only
   when you are ready to take real money).
3. **Developers → Webhooks → Add endpoint**: URL
   `https://ventoryn.app/account/billing/webhook`
   (for local testing: `http://localhost:8000/account/billing/webhook`),
   subscribe to `checkout.session.completed`,
   `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `invoice.payment_succeeded` → copy
   the **Signing secret** → `STRIPE_WEBHOOK_SECRET`.
4. The same webhook URL is what a customer's own Stripe account points
   at for per-site sales: the customer adds it in their Stripe
   dashboard, adds `ventoryn_site_id` = their site id to their
   checkout metadata, and pastes their own webhook signing secret into
   their site's connection page. Sales are then counted automatically;
   the manual "Record a sale" form on the site page is the fallback.

### 3. Environment (server)

```
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
STRIPE_SECRET_KEY=...
STRIPE_WEBHOOK_SECRET=...
STRIPE_PRICE_ID=price_...
SAAS_MONTHLY_CREDITS=100        # optional, default 100
SAAS_BASE_URL=https://ventoryn.app   # default http://localhost:8000
SAAS_COOKIE_SECURE=1            # set to 1 when serving over HTTPS
SAAS_OPERATOR_EMAIL=you@example.com  # the operator's Google email —
    # attaches the existing ecosystem businesses to that account
    # and gates the operator tools. It does NOT open /admin by
    # itself — the admin needs its own password (below).
VENTORYN_ADMIN_PASSWORD=...  # the admin password for /admin —
    # Spencer's value, set on the server, never in chat or the repo.
    # Unset means the admin login always fails.
VENTORYN_TOKEN_KEY=...  # key for encrypting stored tokens
    # (Reddit/X/Plausible/Stripe) and TOTP secrets at rest —
    # base64, hex, or a passphrase. Spencer's value, set on the
    # server, never in chat or the repo. Tokens are refused when no
    # key is configured (fail closed).
VENTORYN_REDDIT_APP_REGISTERED=1  # set to 1 once the Reddit OAuth
    # app exists in the Reddit developer console (Spencer's step)
VENTORYN_X_APP_REGISTERED=1  # set to 1 once the X OAuth app exists
    # in the X developer console (Spencer's step)
```

Restart the dashboard after changing env. The shell's state lives in
the shared state file (`ECOSYSTEM_STATE_DB` when set — the same file as
businesses, ledger and audit); without one the shell runs in-memory,
like the rest of the runtime.

## How it is built

- `core/saas.py` — config from env, `SaasStore` (SQLite tables in the
  shared state file), Google OAuth helpers, Stripe client + webhook
  signature verification (HMAC-SHA256, fail closed) with replay
  protection and idempotent counting (a payment reported twice counts
  once), credit charging (atomic, never negative), Plausible visitor
  fetch, URL validation, the full ISO country list validation.
  Also the admin password session (`VENTORYN_ADMIN_PASSWORD`) with
  rate-limiting + audit, the admin and customer TOTP state, the
  activity tables (last-seen per account, activity events) that power
  the admin's "Live now" list, the connections store (tokens
  encrypted at rest via `core/token_crypto.py`), the revenue-privacy
  toggles, the Ventoryn signature records, and the posting gate
  (`check_posting_allowed`). Stdlib only for HTTP — no new
  dependencies. Webhooks: every payment notice is signature-verified
  (HMAC, fail closed); a sale is attributed to agent work only where
  the Stripe metadata carries the Ventoryn tag, otherwise it is
  unattributed.
- `core/countries.py` — the full ISO 3166-1 alpha-2 list (249 codes),
  `country_options()` renders the selector everywhere the market
  country appears (landing box, /ops analyze panel, site-add).
- `core/token_crypto.py` — stdlib-only authenticated encryption for
  stored tokens (HMAC-SHA256 stream cipher + encrypt-then-MAC, key
  from `VENTORYN_TOKEN_KEY`, fail closed when unset).
- `core/totp.py` — the shared TOTP (RFC 6238, SHA-1, 30s steps, ±1
  step window) + backup codes (salted SHA-256 hashes, one use each).
  One implementation for admin and customers.
- `core/qrcode.py` — stdlib-only QR encoder (byte mode, ECC level L,
  versions 1–10) rendering the 2FA setup QR as SVG.
- `core/signature.py` — the Ventoryn signature: HMAC-signed
  `vtn_<action_id>.<mac>` tags, `sign_url` (UTM + tag), recorded per
  site + agent action.
- `dashboard/saas_routes.py` — the `/account/...` pages (server-rendered
  HTML, mobile-first). Sessions are HttpOnly cookies; only the SHA-256
  hash of the token is stored. Sign-in lives on the landing page;
  the Google callback sends everyone to `/ops`, carrying a typed
  website along as `?site_url=…`. My account shows only the plan
  (name), the websites, and recent jobs — the Subscribe pitch and
  the credit table live only on the Subscribe page; the operator's
  account shows "Permanent — owner" instead of the $20/month plan.
  The 2FA setup/verify pages, the security page (2FA status,
  pause-all-posting, delete account), the site page (connections,
  Data & Privacy controls, signature explainer, conversions), and
  the sensitive-action re-verification (fresh 2FA code when 2FA is
  enabled) live here too. Deleting an account purges its tokens,
  revenue data, and sessions; the caller revokes at the provider first.
- `dashboard/app.py` — the landing page (`/`, public), the scoped
  snapshot (`/api/snapshot`, `/api/report`), the customer's own
  `/portfolio` (colored stat + business cards, not a bare table),
  `/approvals` (scoped + ownership-enforced decide, redirects back
  to the list with a confirmation) and the customer's Acquisition &
  Attribution `/report` (image-3 structure, real data only), the
  operator-only legacy report (old shell, untouched), and the `/admin`
  routes (live now + recent activity, subscribers, accounts with a
  Two-factor column + per-account reset, agents, admin 2FA setup —
  password-gated, see above). The signed-in pages share one centered
  header with a hamburger menu at the far right
  (`dashboard/nav.py`); the forced-2FA gate middleware redirects any
  signed-in session without 2FA to the setup/verify pages. The
  `/admin` look blends the Kay Ayiti admin board (pale slate-blue,
  white radius-16 cards, red count accents) with the ShipItAyiti
  admin (navy/slate headers, ringed cards, uppercase table headers).
  Registers the SaaS routes on the same store/config.
- `dashboard/static/game.html` — the Live Ops page: the shared
  header is injected server-side at the `<!--VENTORYN_HEADER-->`
  placeholder (the pill nav row is gone), the account-aware snapshot
  (operator controls hidden for customers, customer empty states,
  the pending-site subscribe/add-site banner, the `?site_url=`
  prefill), the analyze panel with the full country selector
  (injected at `<!--VENTORYN_COUNTRY_OPTIONS-->`), and a revenue
  figure that renders "hidden" when the snapshot carries null.
- `ecosystem/runtime.py` — `create_dashboard_app()` passes the
  file-backed store.

## What still needs Spencer

- The Google OAuth client (above) and the Stripe product/keys/webhook
  (above). Until they exist, the pages say so plainly instead of
  breaking.
- **Set `VENTORYN_ADMIN_PASSWORD` in the server environment** (his
  own password, typed by him) — until then `/admin` stays locked for
  everyone, including him. **Set `VENTORYN_TOKEN_KEY`** the same way
  (his own value) — until then no connection token can be stored.
  After the first admin login, set up admin 2FA under
  `/admin/two-factor/setup` (scan the QR with his authenticator app,
  save the backup codes).
- **The og-image is intentionally absent** (vector-only rule). If he
  wants link previews to carry an image, that is his call — adding
  one means a bitmap file.
- The **$20/month** price is set. The monthly credit allowance default
  (100) is a starting number — change `SAAS_MONTHLY_CREDITS` if 100 is
  wrong.
- **Where the service runs / its public host.** The redirect URI and
  the webhook URL both depend on it, and hosting is undecided.
- Reddit/X posting: the connection records exist and honestly say "not
  connected — app registration needed". The OAuth app registrations for
  Reddit and X are still to be created, and the posting flow built on
  them.
- A background worker for long jobs: "Analyze my site" runs
  synchronously in the request today. The async job pattern from the
  parallel URL-box work is the natural next step.
- Signup tracking: the funnel shows signups as "unavailable" until a
  signup-tracking connection exists.

## Tests

`tests/test_saas_shell.py` covers: URL validation, sessions, OAuth
states, the Google login flow (mocked HTTP), Stripe webhook signature
+ idempotency + credit grants, customer sale attribution, credit
charging and insufficient-balance refusal, job execution and refunds,
connection honesty (secrets never rendered), and the customer dashboard
(unavailable states, $20/month price). All Stripe/Google calls in tests
are mocked — no live keys, no live charges, ever.

`tests/test_landing.py` covers the landing page and the customer/admin
split: the public landing page (URL box, $20/month, sign-in, no
internal data, no operator links, no invented traction), the sign-in
gates, /ops scoping per account, the customer's own portfolio/report/
approvals (including the 403 when a customer tries to decide someone
else's approval), the /admin pages (operator only, aggregates only, no
customer portfolio or approval content), the nav cleanup, Spencer's
businesses attaching by email config, and the full funnel (URL carried
through sign-in → subscribe → site added) with mocked Google/Stripe.
`tests/test_landing_redesign.py` covers the redesigned landing page
(exact hero copy, navy hero + gradient 100, two-column hero with the
light-panel SVG preview, public nav + Product dropdown, the
illustrative demo data labels, How Ventoryn Works, features,
attribution, pricing from the credit table, 10 FAQs, SEO, vector-only,
no image-2 copy or concept).
`tests/_signin.py` holds the shared sign-in helpers for the auth-gated
dashboard tests.

`tests/test_nav_polish.py` covers the hamburger header (present on
every signed-in page, far-right placement, menu items per role, the
current page as a non-clickable label), the colored portfolio, the
approvals decide flow (pending → Decided with confirmation, 403
ownership case), My account de-duplication + the operator's permanent
subscription, and the admin password gate + live feed + admin look.

`tests/test_countries.py` covers the full ISO country list (249 codes,
default ca), analyze accepting ht/br/ng/jp/fr/us end to end, and
invalid codes rejected.

`tests/test_connections_revenue.py` covers the connections list
(Reddit/X/Plausible/Stripe with honest not-connected states), the
posting gate for unconnected accounts, and the revenue/count/details
toggles (off hides the figures, on shows them; the paying-customer
count still increments while revenue is hidden; attributed
conversions appear in the report with their signature).

`tests/test_security.py` covers tokens (encrypted at rest, never
plaintext; decrypts to the raw value), disconnect revoking at the
provider, the minimal scope strings, webhook bad-signature rejection
and replay idempotency, admin login rate-limiting, and the revenue
toggle hiding all dollar figures from the customer report and the
admin totals while the count still increments.

`tests/test_totp_2fa.py` covers admin 2FA (code required once enabled,
backup codes once-only, operator reset forces setup again) and forced
customer 2FA (fresh account cannot reach /ops or any product page
until setup completes; every route redirects to setup; sign-in with a
valid code works; a backup code works once then fails; a session
deleted server-side is rejected; a customer session cannot open
/admin; sensitive actions demand a fresh 2FA code when 2FA is
enabled) and the corrected sign-in split (any Google account can sign
up and reach /ops + /portfolio + /approvals + /report; the same
account cannot reach /admin without the password).

`tests/test_signature.py` covers the Ventoryn signature (present on
every agent link output and recorded; a payment carrying the signature
is attributed to the right action; a payment without it is
unattributed, never guessed).

`tests/test_vector_only.py` covers vector-only: no page template
references a bitmap image file in rendered HTML (landing, /ops,
/portfolio, /approvals, /report, /account/*, /admin/*).

`tests/test_report_attribution.py` covers the customer report per the
attribution reference: progress ring, Verified / Pending /
Unattributed cards, the attribution flow with evidence checks, the
conversion-status donut, campaign performance, the three privacy
toggles per site, the trend chart from real data, the breakdown bars,
the data-source legend, toggle-aware hiding, and the operator report
unchanged.
