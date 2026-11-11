# Meta + X approval steps — Spencer's console work (Phase 2)

Phase 2 built the code: OAuth adapters, routes, draft approval, and
publishing machinery for X, Facebook, and Instagram are in the tree
and tested. What is **not** built and cannot be built by code is the
providers' own approval of Ventoryn's apps. This document is the
checklist for those console steps. Nothing here spends money except
where it says so, and nothing here turns publishing on — that is a
separate, later decision (see §4).

> **Caveat (from Phase 0):** the prices, limits, and rules below are
> convergent secondary reporting from the three verification docs
> (`X_API_VERIFICATION.md`, `FACEBOOK_API_VERIFICATION.md`,
> `INSTAGRAM_API_VERIFICATION.md`). No official X page could be opened
> in the sandbox and Meta pages mostly returned 403, so re-check the
> cited official pages in a live browser before quoting any figure
> to a customer — especially the $0.20-per-link-post X rate.

## 1. X (first — the shortest clock)

1. **Developer account + project/app.** In the X developer portal,
   create a project/app for Ventoryn and disclose the pilot use case:
   human-approved original posts (POST /2/tweets) via OAuth 2.0 +
   PKCE, posted only after the customer approves each draft in
   Ventoryn. No automated replies, no unsolicited mentions, no DMs.
2. **Enable billing with prepaid credits.** X's API is pay-per-use
   on self-serve. Every pilot post carries a link, so each post is
   expected to bill at the **reported $0.20** (not $0.015). Budget
   for it before the first live post; Ventoryn's own caps
   ($10/day global, $50/month per account, $25/month per site)
   bound what one customer can spend, not what X charges.
3. **Register the redirect URI** in the X app settings, exactly:
   `https://ventoryn.app/account/auth/x/callback`
4. **Note the possible Enterprise scale clause.** The verification
   doc flags an open question: a multi-customer SaaS posting on
   behalf of many accounts may fall under an Enterprise term rather
   than self-serve pay-per-use. Ask X (or watch for it in the
   approval reply) before onboarding a second customer's X account.
5. **Copy the app's client id + secret** into `.env.ventoryn` on
   FedServ (never into chat):
   `VENTORYN_X_CLIENT_ID`, `VENTORYN_X_CLIENT_SECRET`, and
   `VENTORYN_X_APP_REGISTERED=1`.
6. **Verify the live price page** in a browser
   (`docs.x.com/x-api/getting-started/pricing`) — the $0.20 rate
   and the 3M-read cap are reported, not directly verified.

Scopes Ventoryn requests (already in the code, nothing to choose):
`tweet.read tweet.write users.read offline.access`. No DM scope.

## 2. Meta — Facebook + Instagram (one app, the longest clock)

One Meta app covers both networks. The code treats them as one
registration (`app_registered` returns `facebook_configured` for
both).

1. **Create a Business-type Meta app** in the Meta developer
   console (App Dashboard → Create App → Business type).
2. **Register the redirect URIs** (Facebook Login settings),
   exactly:
   `https://ventoryn.app/account/auth/facebook/callback`
   `https://ventoryn.app/account/auth/instagram/callback`
3. **Business Verification.** Meta verifies the legal entity behind
   the app (documents for the business — this is a process cost,
   not a fee). Start this early: it runs on Meta's clock and no
   official fixed duration was found.
4. **Per-permission App Review (Advanced Access).** Each permission
   needs its own submission with a working product demo
   (screencast) and a privacy policy URL. Until a permission clears,
   it works only for people with a role on Ventoryn's Meta app
   (admin/developer/tester) — real customers are blocked.
   - Facebook: `pages_show_list`, `pages_read_engagement`,
     `pages_manage_posts`
   - Instagram: `instagram_business_basic`,
     `instagram_business_content_publish`
   - The customer connecting a Page must hold **CREATE_CONTENT**
     on that Page; the Instagram account must be a Business or
     Creator account linked to a Page the customer manages.
5. **Copy the app id + secret** into `.env.ventoryn`:
   `VENTORYN_FACEBOOK_APP_ID`, `VENTORYN_FACEBOOK_APP_SECRET`.
6. **Note the Instagram attribution constraint** (already in the
   code and the consent text): a URL in a caption is **plain text,
   not clickable**. Ventoryn appends the signed URL to the caption;
   the customer should also put the link in bio (or use a Story
   link sticker) for it to be tappable. Do not promise a tappable
   in-post link on Instagram.

Scopes Ventoryn requests (already in the code):
Facebook `pages_show_list pages_read_engagement pages_manage_posts`;
Instagram `instagram_business_basic instagram_business_content_publish`.
No DM scope ever.

## 3. What the server needs after the consoles

In `.env.ventoryn` on FedServ, after the steps above:

```text
VENTORYN_X_APP_REGISTERED=1
VENTORYN_X_CLIENT_ID=<from the X app>
VENTORYN_X_CLIENT_SECRET=<from the X app>
VENTORYN_FACEBOOK_APP_ID=<from the Meta app>
VENTORYN_FACEBOOK_APP_SECRET=<from the Meta app>
```

Then restart (`python launch.py all`). Until these are set, the
site page honestly says "app registration needed" and the Connect
buttons do not appear — nothing is faked. The X OAuth start will
refuse with the same honest message.

## 4. Turning publishing on (a separate decision — do not do this yet)

Publishing is disabled by default (`VENTORYN_SOCIAL_PUBLISHING_ENABLED`
is unset). Turn it on **only** when:

- the X app is registered + billing is funded, **and/or** the Meta
  permissions above have Advanced Access, **and**
- the t.co question is answered (one live test post, checking the
  signed attribution link survives X's link wrapping), **and**
- Spencer explicitly authorizes production publishing.

Then set `VENTORYN_SOCIAL_PUBLISHING_ENABLED=1` in `.env.ventoryn`
and restart. Even then, nothing publishes without the owning
customer's approval of that exact draft — the machinery enforces
it; there is no bulk-publish path.
