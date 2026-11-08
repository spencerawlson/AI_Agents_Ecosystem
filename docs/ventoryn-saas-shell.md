# Ventoryn SaaS shell

The piece that turns the internal Ventoryn dashboard into a product a
website owner can buy: sign in with Google, subscribe for **$20/month**,
add a website, and Ventoryn works to bring that site its first 100
paying customers. The proof case is Road to CISSP (road2cissp.com).

## What the customer gets

- **The landing page** (`/`) — what Ventoryn is, the URL box with a
  free preview (site score, GEO score, top findings), how it works,
  and the $20/month price. No sign-in needed to read it; it shows no
  internal data and links to no operator page.
- **Sign-in with Google, on the landing page** — no passwords, no
  separate bare sign-in page (`/account/login` redirects to `/`).
  After sign-in EVERYONE goes straight to **Live Ops** (`/ops`) —
  the post-sign-in home for every account, scoped to that account.
- **Their own Live Ops** — the crew, activity feed and stats show
  only that account's sites and their work. No sites yet means an
  empty state with the URL box to add one. The customer nav is:
  Live Ops, My Portfolio, Approvals, Report, My account, Subscribe,
  Sign out.
- **Their own portfolio, report and approvals** — `/portfolio` is
  their sites; `/report` is their growth report (funnel, jobs,
  credits); `/approvals` is their approvals, WITH an approve button.
  Nothing ships until that customer approves it — they pay for it,
  they decide.
- **$20/month plan** — "Ventoryn Growth". (`/account/subscribe`)
- **Credits** — agent work costs credits; the plan includes a monthly
  allowance (see the cost table below). Jobs that cost more than the
  balance are refused with the balance shown; balances never go negative.
- **Website onboarding** — add a site, connect Plausible and the site's
  own Stripe webhook, see the funnel visitors → signups → paying
  customers. Every number comes from a real source; where a connection
  is missing the value reads "unavailable", never invented.
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

**Admin is a separate URL: `ventoryn.app/admin`.** Operator only.
It shows ONLY: who subscribed, all the accounts on the platform,
and what the agents are doing (activity, health, problems). It does
NOT show any customer's portfolio or approvals — those are the
customer's own financial decisions, made alone. No approve-on-behalf,
no per-customer site URLs or funnels in `/admin`. Admin nav:
Subscribers, Accounts, Agents.

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
    # gates /admin + the operator tools, and attaches the existing
    # ecosystem businesses to that account. Spencer's own email goes
    # here; it is never hardcoded in the code.
```

Restart the dashboard after changing env. The shell's state lives in
the shared state file (`ECOSYSTEM_STATE_DB` when set — the same file as
businesses, ledger and audit); without one the shell runs in-memory,
like the rest of the runtime.

## How it is built

- `core/saas.py` — config from env, `SaasStore` (SQLite tables in the
  shared state file), Google OAuth helpers, Stripe client + webhook
  signature verification (HMAC-SHA256, fail closed), credit charging
  (atomic, never negative), Plausible visitor fetch, URL validation.
  Stdlib only for HTTP — no new dependencies.
- `dashboard/saas_routes.py` — the `/account/...` pages (server-rendered
  HTML, mobile-first). Sessions are HttpOnly cookies; only the SHA-256
  hash of the token is stored. Sign-in lives on the landing page;
  the Google callback sends everyone to `/ops`, carrying a typed
  website along as `?site_url=…`.
- `dashboard/app.py` — the landing page (`/`, public), the scoped
  snapshot (`/api/snapshot`, `/api/report`), the customer's own
  `/portfolio` (styled cards, not a bare table), `/approvals` (scoped
  + ownership-enforced decide) and `/report`, the operator-only
  legacy pages, and the `/admin` routes (subscribers, accounts,
  agents). Registers the SaaS routes on the same store/config.
- `dashboard/static/game.html` — the Live Ops page: the customer nav,
  the account-aware snapshot (operator controls hidden for customers,
  customer empty states, the pending-site subscribe/add-site banner,
  the `?site_url=` prefill), the analyze panel untouched.
- `ecosystem/runtime.py` — `create_dashboard_app()` passes the
  file-backed store.

## What still needs Spencer

- The Google OAuth client (above) and the Stripe product/keys/webhook
  (above). Until they exist, the pages say so plainly instead of
  breaking.
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
`tests/_signin.py` holds the shared sign-in helpers for the auth-gated
dashboard tests.
