# X (Twitter) API — Verification for the Ventoryn First-Customer Program (Phase 0, inspection only)

- **Date of research:** 2026-11-08
- **Scope verified:** Only the Ventoryn pilot use case — a SaaS customer connects *their own* X account; Ventoryn's agents research opportunities and draft *original* promotional/informational posts; the customer reviews and approves each post inside Ventoryn; Ventoryn publishes only the approved original post to that customer's own account, with a signed attribution link. No automated replies, no unsolicited mentions, no unsolicited DMs, no artificial engagement, no mass posting.
- **Method and its limit — read this first:** Direct fetching of the official X documentation was **blocked in this runtime**. `browser.open` on `https://docs.x.com/`, `https://developer.x.com/` and `https://help.x.com/en/rules-and-policies/x-automation` each failed with "Domain … is blocked by policy" (no HTTP status returned). That failure was not retried, per the runtime instruction. **No official X page was read directly in this session.** Every "official" fact below is therefore reported **indirectly**: it comes from web-search results that quote, paraphrase, or cite the named official page, cross-checked across multiple independent secondary sources that state they read that official page in Sep–Nov 2026. Where several independent checkers converge on the same figure/rule and name the same official page, that is noted as *convergent secondary reporting of the official page*. Where they disagree or only one source says it, that is flagged. **Nothing in this document counts as a direct official verification until someone with live browser access opens the cited official URL and confirms the text.** That re-check is the single most important follow-up (see "Unverified / open questions").
- **Official sources named in the task (all cited below; none directly opened):**
  - https://docs.x.com/
  - https://developer.x.com/
  - https://help.x.com/en/rules-and-policies/x-automation
- **Other official page paths cited below** (paths as they appeared in search results / secondary sources citing docs.x.com; not directly opened in this session):
  - https://docs.x.com/x-api/getting-started/pricing
  - https://docs.x.com/x-api/getting-started/getting-access
  - https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code
  - https://docs.x.com/fundamentals/authentication/overview
  - https://docs.x.com/x-api/posts/create-post
  - https://docs.x.com/x-api/fundamentals/rate-limits
  - https://docs.x.com/x-api/media/introduction
  - https://docs.x.com/x-api/media/quickstart/media-upload-chunked
  - https://docs.x.com/developer-terms (Developer Agreement / Developer Policy / Restricted Use Rules)
  - https://docs.x.com/changelog
- **Currency:** All prices in USD unless stated. Prices are reported as "subject to change" on the official pricing page; the Developer Console is reported as the live source of truth.
- **No code changes, no spending, no credentials, no live actions were taken for this document.**

## Verdict

**Pilot use case is plausibly supported on X's self-serve pay-per-use API, but it is NOT cheap when posts carry a link, and it is NOT directly verified from official pages in this session.**

- Original, human-approved posts to the customer's own account via `POST /2/tweets` with OAuth 2.0 + PKCE are the core, supported path. Automated posting of informational/promotional content is allowed under X's Automation Rules (updated April 2026), provided it is not spam/duplicative and uses the official API, not website scripting.
- The dominant cost is the link: a plain post is reported at **$0.015**, a post containing a URL at **$0.20** (13.3×) — and Ventoryn's pilot posts carry a signed attribution link, so **every pilot post is expected to bill at $0.20, not $0.015**, unless X treats the attribution link differently (no official evidence found that it does). Research reads add $0.005 per post returned and $0.010 per user read. There is no free tier for new developers and no monthly subscription; credits are prepaid in the Developer Console, and pay-per-use post reads are capped at 3,000,000 per monthly billing cycle (Enterprise above that).
- Replies are **out of scope and effectively closed** for the pilot on self-serve: since 23 February 2026, API replies are accepted only when the original author explicitly summoned the replying account (@mention or quote), and AI reply bots additionally require X's prior written and explicit approval. Multiple Oct–Nov 2026 sources report the summoned-reply rule is still current.
- **Blockers / gates before any build or spend:** (1) direct re-verification of the pricing page and the Automation Rules in a live browser (blocked here); (2) a developer account + project/app + use-case disclosure, with billing/credits enabled — pay-per-use is the only self-serve route for new developers since 6 February 2026; (3) confirmation in the live console of the $0.20-with-URL charge for a signed attribution link and whether any link/URL type is exempt; (4) confirmation of whether Ventoryn's multi-customer scale (many authenticated users on one app) triggers X's Enterprise requirement — the Developer Policy is reported to require an Enterprise plan if unique authenticated users "greatly exceed" normal levels for a similar tier, on written notice from X. No separate per-post application review was found for ordinary original posting, but the use-case description given at developer-account application is binding and substantive changes need prior approval.
- **What is not verified officially in this session:** every price, rate limit, scope, token lifetime, media limit, and policy quotation below (see the "Unverified / open questions" section for the full list). Do not treat any figure here as a contractual commitment by X.

---

## 1. Current API consumption pricing

**Source (official page named by secondary sources):** https://docs.x.com/x-api/getting-started/pricing

**Verification status:** **NOT directly verified.** The pricing page could not be opened in this session (domain blocked). The figures below are convergent secondary reporting: at least four independent checkers (postzen.dev, superframeworks.com, imisofts.com, twitterapi.io, outstand.so, xautopilot.app) state they read the same official pricing page in Sep–Oct 2026 and report identical figures. That convergence is strong but is not a direct official read.

- **Model:** Pay-per-use / pay-per-usage only for new developers since **6 February 2026**. No subscriptions, no monthly minimum, no contracts — "pay only for what you use". Credits are bought prepaid in the Developer Console (console.x.com) and deducted per request; a zero balance returns HTTP 402. The old Free tier (closed to new developers 6 Feb 2026), Basic ($200/mo) and Pro ($5,000/mo) are reported gone/deprecated for new sign-ups; remaining Basic subscribers were migrated to pay-per-use during 2026 (Pro deprecation announced 14 Aug 2026). Source: https://docs.x.com/x-api/getting-started/pricing (via secondary reporting, Sep–Oct 2026).
- **Reference amounts — all reported, none directly verified:**
  - Standard post creation: **$0.015 per request** — reported consistently. *Status: reported, not directly verified.*
  - Post containing a URL: **$0.20 per request** — reported consistently. *Status: reported, not directly verified.*
  - Retrieved post (post read): **$0.005 per post returned** — reported consistently. *Status: reported, not directly verified.*
  - Retrieved user (user read): **$0.010 per user/resource** — reported consistently. *Status: reported, not directly verified.*
  - Summoned reply: **$0.010 per request** — reported consistently. *Status: reported, not directly verified.*
- **What is metered:** Reads are billed **per resource returned**, not per API call (e.g. a search returning 100 posts bills 100 × $0.005). Writes are billed **per request**. A post with a URL bills at the $0.20 rate per post, not per link. Source: https://docs.x.com/x-api/getting-started/pricing (via secondary reporting).
- **Deduplication:** The same resource requested more than once inside a 24-hour UTC window is reported to be charged only once. Source: https://docs.x.com/x-api/getting-started/pricing (via secondary reporting).
- **Owned reads:** Reads of your own app/account's data are reported at **$0.001 per resource** ("1,000 resources for $1"), effective 20 April 2026, for 12 listed endpoints, only when the requesting user ID is the authenticated user and that user owns the developer app. Source: https://docs.x.com/x-api/getting-started/pricing (via secondary reporting).
- **Monthly caps / allowances:** Pay-per-use is reported capped at **3,000,000 post reads per monthly billing cycle**; above that X points to Enterprise (no public price). Note: some sources checked earlier in 2026 report 2,000,000; multiple Oct 2026 sources report the cap rose from 2M to 3M — treat 3M as the later-reported figure, **unverified directly**. No free tier for new developers as of Sep–Oct 2026. One-time free credits of up to $70 are reported as rolling out since 2 Oct 2026 ($20 for saving a first eligible card + completing a first purchase, plus a match of the first auto-recharge up to $50) — reported, not directly verified, and eligibility conditions (prepaid cards excluded; accounts with a prior card/payment excluded) are also only second-hand. Source: https://docs.x.com/x-api/getting-started/pricing and https://docs.x.com/changelog (via secondary reporting).
- **Price-change history (reported):** Effective **20 April 2026**, plain post creation rose from $0.010 to $0.015, posts with a URL were set at $0.20, summoned replies stayed at $0.010, owned reads were cut to $0.001, and follows/likes/quote posts were removed from all self-serve tiers (changelog entry 16 Apr 2026). Source: https://docs.x.com/changelog (via secondary reporting).
- **Currency:** USD (all figures quoted in $). No non-USD pricing found.
- **Cost illustration (arithmetic from the reported rates, not a quote):** 1 approved plain post/day = 30 × $0.015 = $0.45/mo. 1 approved post/day *with a link* = 30 × $0.20 = $6.00/mo. 100 link posts/day = 3,000 × $0.20 = $600/mo. Research of 500 matching posts/day via recent search = ~15,000 posts/mo × $0.005 = $75/mo (or $15/mo at the $0.001 owned-read rate if the owned-read conditions apply, which they would not for third-party opportunity research).

## 2. Developer registration requirements

**Sources:** https://docs.x.com/x-api/getting-started/getting-access ; https://docs.x.com/ ; https://developer.x.com/

**Verification status:** Not directly verified (pages blocked). Steps below are as reported from the official Getting Access guide by multiple secondary sources (Sep–Oct 2026).

- Sign in at the Developer Console, **console.x.com**, with an X account (reported to need a verified email and a verified phone number; accounts missing either are reported blocked before reaching the console). The developer account, its apps, credits and usage history belong to that X account. Source: https://docs.x.com/x-api/getting-started/getting-access (via secondary reporting).
- Accept the **Developer Agreement and Policy** on first entry; no app can be created until accepted. Source: https://docs.x.com/x-api/getting-started/getting-access (via secondary reporting).
- Provide a **written use-case description** as part of the developer-account application (see §3 — this description is binding).
- Create a **Project**, then an **App inside the project**. Apps live inside projects; the app is the object that issues credentials. Each app carries its own credential set. Source: https://docs.x.com/x-api/getting-started/getting-access (via secondary reporting).
- From the app's **Keys and Tokens** tab, copy the API key/secret, Bearer token and OAuth credentials. Source: https://docs.x.com/x-api/getting-started/getting-access (via secondary reporting).
- Configure **User authentication settings**: app permissions (Read / Read and write / Read, write and Direct Messages for OAuth 1.0a; OAuth 2.0 scopes and client type), callback/redirect URLs, and app type (Web or Native for OAuth 2.0 user context; "Automated" is reported as app-only). For posting, permissions must be **Read and write**. Source: https://docs.x.com/fundamentals/authentication/overview (via secondary reporting).
- **Tier / access level needed for posting:** Pay-per-use (self-serve) is reported as the default and only self-serve route for new developers since 6 Feb 2026; Enterprise is sales-led/custom-priced. Posting (`POST /2/tweets`) is reported available on pay-per-use. No separate "posting tier" was found. Source: https://docs.x.com/x-api/getting-started/pricing (via secondary reporting).
- **Billing:** Enable billing and load credits in the console before paid requests succeed; until the project is approved/billing is on, calls are reported to fail rather than bill. An app not attached to a project with pay-per-use enabled is reported to return `client-not-enrolled`. Source: https://docs.x.com/x-api/getting-started/getting-access (via secondary reporting).

## 3. Whether application review or additional approval is required for the intended use

**Sources:** https://docs.x.com/developer-terms (Developer Policy) ; https://docs.x.com/x-api/getting-started/getting-access ; https://help.x.com/en/rules-and-policies/x-automation

**Verification status:** Not directly verified. The Developer Policy passage below is quoted in a secondary mirror (x-preview.mintlify.app, reporting the current Developer Policy text) and in a compliance summary dated 2026-09-04; the Automation Rules approval point is in §11/§12.

- **Developer-account application is itself a review:** "We review all proposed uses of the X developer platform to verify policy compliance — so you're required to disclose (and update, as applicable) your planned use of the X API and X Content in order to be granted and to maintain access. All new developers must apply for a developer account to access the X API… you'll need to provide us with a written description of your intended uses… Your use case description is binding on you, and any substantive deviation from it may constitute a violation… You must notify us of any substantive modification to your use case and receive approval before you may begin using X Content for that new purpose." Source: https://docs.x.com/developer-terms (Developer Policy, via secondary quotation).
- **No separate per-post or per-app "app review" (like a mobile app-store review) for ordinary original posting was found** in the sources consulted. The gate is the developer-account/use-case approval above, plus compliance with the Automation Rules and Developer Policy.
- **Scale gate:** "Pay-Per-Use access… is designed for hobbyists, commercial prototyping, early-stage X product integrations, and supporting applications with limited end-users. If you use the X API and X Content beyond this scope, then you must apply (or already subscribe to) an Enterprise plan." X also "reserves the right to monitor the total number of unique users who have authenticated… and, in the event that total number greatly exceeds the normal levels of other developers subscribed to a similar tier of service, you must apply (or already subscribe to) an Enterprise plan upon written notice from X." Source: https://docs.x.com/developer-terms (Developer Policy, via secondary quotation). **Implication for Ventoryn:** a multi-customer SaaS (many end users authenticating via one app) is exactly the fact pattern this clause addresses. The threshold ("greatly exceeds normal levels") is not quantified in the quoted text — **unverified / open question**; confirm with X / the console before scaling beyond the pilot.
- **No multiple apps for one use case:** "You may not register multiple applications for a single use case or substantially similar or overlapping use cases… 'White label' versions of a tool or service are not permissible" without notifying X (passage truncated in the secondary source — full text not verified). Source: https://docs.x.com/developer-terms (via secondary quotation).
- **Separate approvals that DO apply to adjacent features (not the pilot core):** AI reply bots need prior written approval (§12); auto-response campaigns by advertisers/publishers/brands must request approval (§11). Neither applies to human-approved original posts, but both would apply if replies were added later.

## 4. Available endpoints and permitted use cases for posting, reading posts/users, and recent search

**Sources:** https://docs.x.com/x-api/posts/create-post ; https://docs.x.com/x-api/fundamentals/rate-limits ; https://docs.x.com/ (API reference / OpenAPI)

**Verification status:** Endpoints and paths below are consistently reported across many secondary sources citing the official reference; none directly opened in this session. Base URL reported as `https://api.x.com` (legacy `https://api.twitter.com` still resolves for the same `/2/...` paths).

| Use | Endpoint | Auth (reported) | Notes |
|---|---|---|---|
| Create a post (original, reply, or quote via fields) | `POST /2/tweets` | OAuth 2.0 user context (scopes `tweet.read` + `tweet.write` + `users.read`) or OAuth 1.0a user context; app-only Bearer token rejected | Body fields reported: `text`, `media`, `poll`, `reply`, `quote_tweet_id`, `community_id`, `edit_options`. No scheduling parameter — no post-scheduling endpoint was found; scheduling must be done client-side (fire the request at publish time). See §8. Source: https://docs.x.com/x-api/posts/create-post |
| Delete a post | `DELETE /2/tweets/{id}` | User context | Reported. Source: https://docs.x.com/x-api/posts/create-post (via secondary reporting) |
| Look up a single post | `GET /2/tweets/{id}` | App-only or user context | Reported 450/15 min per app, 900/15 min per user. Source: https://docs.x.com/x-api/fundamentals/rate-limits |
| Look up multiple posts by ID | `GET /2/tweets` (up to 100 IDs) | App-only or user context | Reported 3,500/15 min per app, 5,000/15 min per user. Source: https://docs.x.com/x-api/fundamentals/rate-limits |
| Look up a user by ID | `GET /2/users/{id}` | App-only OK | Reported 300/15 min per app, 900/15 min per user. Source: https://docs.x.com/x-api/fundamentals/rate-limits |
| Look up users by username | `GET /2/users/by` (1–100 usernames); `GET /2/users/by/username/{username}` | App-only or user context | Reported. Source: https://docs.x.com/ (API reference, via secondary reporting) |
| Authenticated user | `GET /2/users/me` | User context only (OAuth 2.0 or 1.0a); app-only Bearer token not sufficient; scopes `users.read`, `tweet.read` | Reported. Source: https://docs.x.com/ (API reference, via secondary reporting) |
| User timeline | `GET /2/users/{id}/tweets` | App-only or user context | Reported up to ~3,200 recent posts. Source: https://docs.x.com/ (API reference, via secondary reporting) |
| Mentions of a user | `GET /2/users/{id}/mentions` | User context | Reported up to ~800. Source: https://docs.x.com/ (API reference, via secondary reporting) |
| Recent search | `GET /2/tweets/search/recent` | App-only or user context | Last 7 days; see §9. Source: https://docs.x.com/ (API reference, via secondary reporting) |
| Full-archive search | `GET /2/tweets/search/all` | App-only or user context | Back to March 2006; access reported as requiring an access upgrade on pay-per-use or Enterprise — see §9 (conflicting reports). Source: https://docs.x.com/ (API reference, via secondary reporting) |
| Media upload (v2) | `POST /2/media/upload` (one-shot, images/subtitles) and `POST /2/media/upload/initialize`, `/append`, `/finalize`, `GET /2/media/upload?command=STATUS` (chunked) | OAuth 2.0 user context with `media.write`, or OAuth 1.0a | See §13. Source: https://docs.x.com/x-api/media/introduction |

- **Removed from self-serve (reported, Apr 2026):** Follow (`POST /2/users/{id}/following`), like (`POST /2/users/{id}/likes`) and quote-post (`quote_tweet_id` on `POST /2/tweets`) endpoints were removed from all self-serve tiers on 16 Apr 2026 / effective 20 Apr 2026; the Create Post reference is reported to carry a warning that `quote_tweet_id` requires an Enterprise plan. **Implication:** Ventoryn must not plan likes, follows, or quote-posts on pay-per-use. Source: https://docs.x.com/changelog ; https://docs.x.com/x-api/posts/create-post (via secondary reporting).
- **No scheduling endpoint:** A Oct 2026 review of the full docs index (`docs.x.com/x-api/llms.txt`, 370+ pages) found no dedicated post-scheduling endpoint or future-publication parameter on Create Post; the only scheduling feature found is a Livestream Scheduling API for broadcasts. Ventoryn must hold approved drafts and call `POST /2/tweets` at publish time. Source: https://docs.x.com/ (via secondary reporting, Oct 2026).

## 5. OAuth authentication methods

**Sources:** https://docs.x.com/fundamentals/authentication/overview ; https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code

**Verification status:** Not directly verified. Consistently reported across secondary sources.

- **OAuth 2.0 Authorization Code Flow with PKCE is supported and is the recommended / modern route for user-context actions** (posting, reading on a user's behalf). It is reported as the only supported OAuth 2.0 grant for user context, with grant types `authorization_code` + `refresh_token` only. PKCE (S256 preferred) is required — the authorization endpoint is reported to return `invalid_request` if `code_challenge` + `code_challenge_method` are omitted; a plain client-secret-only flow does not work for user context. Authorize URL reported as `https://x.com/i/oauth2/authorize` (also seen as `https://x.com/i/oauth2/authorize`); token endpoint `POST https://api.x.com/2/oauth2/token`. App type must be **Web or Native** in the console, not "Automated" (reported as app-only). Source: https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code (via secondary reporting).
- **App-only (OAuth 2.0 Client Credentials / Bearer token):** Authenticates the app itself; reaches only public data (reads such as user lookup, recent search, trends). It **cannot** create posts or perform any write. Source: https://docs.x.com/fundamentals/authentication/overview (via secondary reporting).
- **OAuth 1.0a user context:** The older per-user, request-signing scheme (consumer key/secret + user access token/secret). It is reported as still accepted by essentially all user-context v2 endpoints (post/delete, likes, follows, DMs, etc.) and as the traditional signer for media upload. One secondary source (Sep 2026) reports X is retiring it and that since 21 Sep 2026 stored 1.0a tokens can be converted to OAuth 2.0 silently via a token-exchange grant — **that retirement/exchange claim is single-source and unverified**. Another source reports X removed OAuth 1.0a support for most write endpoints in 2023–2024 — this conflicts with the "still accepted" reporting and is **unresolved**; the pilot should not depend on 1.0a.
- **Where OAuth 1.0a is still required:** On the evidence gathered, **no v2 endpoint needed for the pilot core (posting, reading, recent search, v2 media upload) requires OAuth 1.0a** — OAuth 2.0 + PKCE covers all of them (media upload via `media.write`). OAuth 1.0a is reported as required only for the **legacy v1.1 media upload** host (`upload.x.com/1.1/media/upload.json`), which was deprecated for self-serve on 31 Mar 2025 (extended to 9 Jun 2025 in one source) and should not be used for new work. Some sources report v1.1 still works in practice with no announced sunset; others report it closed for self-serve — **unresolved, and irrelevant to the pilot if v2 media upload is used**. The X Ads API REST (not in pilot scope) is reported to use OAuth 1.0a. Source: https://docs.x.com/x-api/media/introduction ; https://docs.x.com/fundamentals/authentication/overview (via secondary reporting).
- **Recommendation for the pilot:** OAuth 2.0 Authorization Code with PKCE, confidential or public client as appropriate, with `offline.access` (§7). Do not build on OAuth 1.0a.

## 6. Required OAuth scopes for posting + reading

**Sources:** https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code ; https://docs.x.com/x-api/posts/create-post

**Verification status:** Not directly verified; consistently reported.

- **Minimum practical set for the pilot (posting + reading):** `tweet.read tweet.write users.read offline.access`, plus `media.write` if posts carry images/video.
  - `tweet.read` — read posts; reported as needed even for write flows (most write ops verify the post exists first). Source: https://docs.x.com/x-api/posts/create-post
  - `tweet.write` — create (and delete) posts on the user's behalf. Source: https://docs.x.com/x-api/posts/create-post
  - `users.read` — read user profiles, including the authenticated user. Source: https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code
  - `offline.access` — required to receive a refresh token (§7). Source: https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code
  - `media.write` — upload media via `POST /2/media/upload` (v2). Without it, text posts work but media upload returns 403; a token issued before `media.write` was added stays scopeless until the user re-authorizes. Source: https://docs.x.com/x-api/media/introduction
- **Scopes are granular and additive;** the consent screen shows only what was requested; the scopes enabled in the app's User authentication settings define the maximum the app can request, and the token gets whatever subset the user consents to at authorize time. A call missing the right scope returns 403. Source: https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code (via secondary reporting).
- **Scopes NOT needed for the pilot (do not request):** `follows.read/write`, `like.read/write`, `dm.read/write`, `list.read/write`, `bookmark.read/write`, `space.read`, `mute.read/write`, `block.read/write`, `tweet.moderate.write`, `broadcast.read/write`, `users.email`. Requesting them would over-scope the consent screen and, for follows/likes, point at endpoints removed from self-serve (§4). The full reported scope list has 24 entries; see the "Unverified" section — the exact current list was not directly read.
- **App permission level:** In User authentication settings the app must be set to **Read and write** for posting (Read only is insufficient; Read, write and Direct Messages is only needed if DMs are used, which they are not in the pilot). Source: https://docs.x.com/fundamentals/authentication/overview (via secondary reporting).

## 7. Access token and refresh token behavior

**Sources:** https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code ; https://docs.x.com/fundamentals/authentication/overview

**Verification status:** Not directly verified; consistently reported across secondary sources.

- **OAuth 2.0 user access token lifetime: ~2 hours** (`expires_in` typically 7200 seconds). Source: https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code (via secondary reporting).
- **Refresh token:** Issued **only** when the `offline.access` scope is requested. Without it, there is no refresh token and the user must re-consent every ~2 hours. With it, the access token can be renewed without re-prompting the user via `POST https://api.x.com/2/oauth2/token` with `grant_type=refresh_token`, `refresh_token`, and `client_id` (plus client secret for confidential clients). Source: https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code (via secondary reporting).
- **Refresh-token rotation:** Refresh tokens are reported to **rotate — each refresh returns a new refresh token and the old one is single-use**. Multiple sources advise: persist the new refresh token *before* using the new access token, serialize refreshes per user to avoid races, and refresh proactively (~90 minutes after issue) before expiry. If refresh fails, force re-authorization. Source: https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code (via secondary reporting — rotation is reported by secondary sources; whether the official docs state a fixed refresh-token lifetime was **not verified**).
- **Refresh-token lifetime / expiry:** **Not verified.** Sources describe the refresh token as "long-lived" / valid "until revoke / rotate fail" — no fixed duration (e.g. 6 months) was found in the sources consulted. Do not assume a fixed lifetime.
- **Revocation:** Tokens die when the user removes the app / revokes in X settings (next API call returns 401 — re-prompt to reconnect), when invalidated via the revoke endpoint (`POST /2/oauth2/revoke` / `oauth2/invalidate_token`), or when the app secret is changed. OAuth 1.0a user tokens are reported not to expire (no clock) but to die on regeneration/invalidation/revocation — pilot does not use 1.0a. App-only Bearer tokens have no clock but die on regeneration. Source: https://docs.x.com/fundamentals/authentication/overview (via secondary reporting).
- **Single live grant (reported, Ads MCP docs — may not apply to the core API):** X is reported to keep a single live OAuth grant per (app, user) in at least one product surface — signing in from a second client revokes the first client's tokens. Whether this applies to the core X API v2 OAuth 2.0 flow was **not verified**; flag for live testing (a customer re-connecting from a second device must not silently revoke the stored grant).
- **Practical consequence for Ventoryn:** A scheduler cannot store a token at connect time and use it at publish time; it must refresh before every publish job. Token storage must be encrypted, and refresh must be serialized per user.

## 8. Rate limits and monthly consumption constraints (per-endpoint and per-app)

**Sources:** https://docs.x.com/x-api/fundamentals/rate-limits ; https://docs.x.com/x-api/getting-started/pricing

**Verification status:** Not directly verified; figures below are as reported from the official rate-limit tables (re-checked by secondary sources Oct 2026). Billing and rate limits are reported as **separate** concerns: paying does not raise a rate limit, and hitting a limit costs nothing extra.

| Endpoint | Per app | Per user | Window / notes |
|---|---|---|---|
| `POST /2/tweets` (create post) | 10,000 / 24 h | 100 / 15 min | The app-level 10,000/day is the number to plan around — it is reported as shared across every account connected to the app (one secondary source disputes whether the per-app figure applies to user-token posts; treat as shared until verified in the console). Source: https://docs.x.com/x-api/fundamentals/rate-limits |
| `GET /2/tweets/search/recent` | 450 / 15 min | 300 / 15 min | 100 max results per request. Source: https://docs.x.com/x-api/fundamentals/rate-limits |
| `GET /2/tweets/search/all` (full-archive) | 300 / 15 min | 300 / 15 min | Plus a 1-request-per-second ceiling. Source: https://docs.x.com/x-api/fundamentals/rate-limits |
| `GET /2/tweets` (lookup, up to 100 IDs) | 3,500 / 15 min | 5,000 / 15 min | Source: https://docs.x.com/x-api/fundamentals/rate-limits |
| `GET /2/tweets/{id}` | 450 / 15 min | 900 / 15 min | Source: https://docs.x.com/x-api/fundamentals/rate-limits |
| `GET /2/users/{id}` | 300 / 15 min | 900 / 15 min | Source: https://docs.x.com/x-api/fundamentals/rate-limits |
| Media upload initialize / append / finalize | 1,875 / 15 min each; 180,000 / 24 h (reported) | — | Counts calls, not files; a 1 GB video in 4 MB chunks is ~256 append calls. One-shot `POST /2/media/upload` reported 500 / 15 min. Source: https://docs.x.com/x-api/fundamentals/rate-limits |

- **Exceeding a limit:** HTTP **429** (error code 88 reported) until the window resets. Responses carry `x-rate-limit-limit`, `x-rate-limit-remaining`, and `x-rate-limit-reset` (Unix timestamp) headers. Source: https://docs.x.com/x-api/fundamentals/rate-limits (via secondary reporting).
- **Monthly consumption constraint:** Pay-per-use post reads capped at **3,000,000 per monthly billing cycle** (see §1; earlier 2M figure also reported — unresolved which is current without a direct read). Enterprise above that; Enterprise limits are custom. Source: https://docs.x.com/x-api/getting-started/pricing (via secondary reporting).
- **Account-level daily limits (separate from API limits, from X's consumer Help Center — not directly verified):** Unverified accounts are reported limited to 50 original posts and 200 replies per day (May 2026 Help Center update); the Help Center "About X limits" page is also reported to list a 2,400-posts-per-day account cap broken into semi-hourly intervals, 500 DMs/day, 400 follows/day. These account-level caps apply per customer account, not per app, and include actions from all devices/API. **Not verified directly; the two post figures (50/day vs 2,400/day) appear contradictory and likely reflect different account states (unverified vs general) — open question.** Source: https://help.x.com/ (About X limits, via secondary reporting).
- **No post-scheduling endpoint** (§4) — rate limits therefore apply at the moment of the publish call, not at draft/approval time.

## 9. Recent search capabilities (window, limits)

**Sources:** https://docs.x.com/ (search reference) ; https://docs.x.com/x-api/fundamentals/rate-limits

**Verification status:** Not directly verified; consistently reported.

- **Endpoint:** `GET /2/tweets/search/recent`. **Window: last 7 days only** (rolling window, not calendar). A job that runs weekly and misses a day loses that day permanently unless full-archive search is used to backfill. Source: https://docs.x.com/ (search reference, via secondary reporting).
- **Results:** 10 default, **100 max per request** (`max_results` 10–100). Query up to **512 characters**. Works with app-only or user-context auth. Source: https://docs.x.com/ (search reference, via secondary reporting).
- **Rate limit:** 450 requests / 15 min per app, 300 / 15 min per user. Source: https://docs.x.com/x-api/fundamentals/rate-limits.
- **Cost:** Every post returned bills as a post read at $0.005 (or $0.001 owned-read rate where applicable — not applicable to third-party opportunity research). Source: https://docs.x.com/x-api/getting-started/pricing.
- **Full-archive search:** `GET /2/tweets/search/all`, back to March 2006, 10 default / **500 max per request**, 300 / 15 min plus 1 req/sec. **Access is disputed in secondary sources:** some report it is available on pay-per-use with a self-serve access upgrade; others report it is Enterprise-only / requires an Enterprise contract (one source cites $42,000/mo as the Enterprise starting point — that figure is **unverified and single-source**). The pilot's research window is recent opportunities, so full-archive access is **not required for the pilot**; do not assume it is available on pay-per-use. Source: https://docs.x.com/ (search reference, via secondary reporting).
- **Search operators** (reported): `from:`, `to:`, `lang:`, etc. Full operator list not verified in this session.
- **Filtered stream** (for completeness, not pilot scope): `GET /2/tweets/search/stream` with rules at `POST /2/tweets/search/stream/rules`; reported as available on pay-per-use, each delivered post billing $0.005 and counting toward the 3M cap, 1 connection and 1,000 rules per project. Source: https://docs.x.com/ (via secondary reporting).

## 10. Mention and reply restrictions — is the February 2026 rule still current?

**Sources:** https://docs.x.com/x-api/posts/create-post (Manage Posts / "Post creation restrictions (self-serve)") ; https://docs.x.com/changelog (23 Feb 2026) ; https://help.x.com/en/rules-and-policies/x-automation

**Verification status:** Not directly verified (official pages blocked). Reported as still current by multiple sources checked Oct–Nov 2026, including a pricing-page update dated 3 Oct 2026 that still lists the summoned-reply price.

- **The rule (as quoted in secondary sources from the official docs/changelog):** "Replies via the API are only permitted if the replying account has been explicitly summoned by the original post's author." Summoned means exactly one of: (a) the original author **@mentions** the replying account in their post, or (b) the original author **quotes** a post from the replying account. If neither is true, the request is rejected. Source: https://docs.x.com/changelog (23 Feb 2026) ; https://docs.x.com/x-api/posts/create-post (via secondary reporting).
- **Scope:** Applies to **self-serve tiers only** — legacy Free, Basic, Pro and pay-per-use. **Enterprise access is not affected.** Source: https://docs.x.com/changelog (via secondary reporting).
- **Error:** A non-summoned reply is reported to return **403 `not-authorized-for-resource`** with the detail "You can only reply to or quote posts where you are mentioned or are the author." Replies to your own posts are reported exempt (threads — replying to yourself — are not the concern). Source: https://docs.x.com/x-api/posts/create-post (via secondary reporting).
- **Mention/quote restrictions (same Feb 2026 change, plus 28 Feb 2026):** Further restrictions apply to programmatically mentioning or quoting users — the changelog is reported not to list them in full. A separate 403 is reported for ordinary non-reply posts: "Creating posts with @mentions is not allowed for your access package. In replies to conversations you are part of, you may only @mention users who are already participants in the thread." And `quote_tweet_id` is reported to require Enterprise on self-serve (§4). Source: https://docs.x.com/changelog (via secondary reporting).
- **Still current?** **Yes, as far as secondary reporting can establish.** Sources checked Oct 2026 state the restriction is still in force; the official pricing page as of 3 Oct 2026 is reported to still list "Post: Create (summoned)" at $0.010, which only exists because the summoned-reply pathway is the live reply pathway on self-serve. No source reports a repeal. **Direct confirmation still required** (open the Manage Posts page in a live browser).
- **Unresolved edge:** Whether a comment under your own post counts as summoning you (i.e. whether replying to a commenter on your own post is accepted) is reported as **not stated** by X — test on your own account before relying on it. Source: https://docs.x.com/x-api/posts/create-post (via secondary reporting).
- **Pilot impact:** None for the core pilot (original posts only). Replies are out of scope and must stay out of scope on pay-per-use unless a summoned context is proven per reply.

## 11. Automated posting requirements and automation policy constraints

**Source:** https://help.x.com/en/rules-and-policies/x-automation (Automation Rules, marked "Updated April 2026")

**Verification status:** Not directly verified (help.x.com blocked). The rules below are as quoted/paraphrased by multiple secondary sources that state they read the page (Aug–Oct 2026).

- **Definition:** Automation = accounts or apps that take repeated actions without a person actively performing them. Source: https://help.x.com/en/rules-and-policies/x-automation (via secondary reporting).
- **Allowed (reported):** Automated posts, including posts that cross-post outside information such as an RSS feed, and posts "for informational, entertainment or novelty purposes" — provided they are not spam or duplicative. Scheduled/helpful posts are welcomed. Source: https://help.x.com/en/rules-and-policies/x-automation (via secondary reporting).
- **Allowed with conditions — automated replies and mentions:** Only to people who have asked for them / opted in (e.g. by replying to the account's post or messaging it), with an easy opt-out, and **one automated reply/mention per user interaction**. Following the account does **not** count as opt-in. For campaigns based on replies to your post, the reply must go to the user's original post. Source: https://help.x.com/en/rules-and-policies/x-automation (via secondary reporting).
- **Not permitted (reported):** Sending automated replies to posts based on **keyword searches alone**; automated **likes**; automatic posts about **trending topics**; **duplicate posts across accounts**; unsolicited automated replies/mentions/DMs; bulk following; scripting the **x.com website** instead of using the API; any automation that circumvents rate limits. Source: https://help.x.com/en/rules-and-policies/x-automation (via secondary reporting).
- **Needs X's approval:** AI-powered reply bots (see §12); advertisers, publishers and brands running **auto-response campaigns** must request approval. Source: https://help.x.com/en/rules-and-policies/x-automation (via secondary reporting).
- **Consent / responsibility:** OAuth authorization alone is reported **not** to count as consent for automated actions on a user's account — the app must describe the actions clearly, get express consent for them, and honour an opt-out immediately. Account owners are responsible for content posted by authorised admins/contributors. Bot accounts must clearly state what they are and who runs them. Source: https://help.x.com/en/rules-and-policies/x-automation (via secondary reporting).
- **Pilot fit:** Human-approved original posts via the official API, one post per approval, no keyword-triggered replies, no likes/follows, no website scripting — this sits inside the "allowed" category as reported. The pilot's per-post human approval is stronger than the minimum (automation alone would be allowed for original posts); keep that approval step as the compliance story. The Developer Policy adds: services that perform write actions (posting, following, sending DMs) must follow the Automation Rules. Source: https://docs.x.com/developer-terms (via secondary quotation).

## 12. AI-generated reply policy (replies are out of scope for the pilot)

**Source:** https://help.x.com/en/rules-and-policies/x-automation (section "AI-Powered Automated Replies", page updated April 2026)

**Verification status:** Not directly verified. The quoted sentence below is reproduced identically across many secondary sources.

- **Policy (as quoted):** "Provided you comply with all other rules, you may leverage artificial intelligence (AI) technologies to create automated reply bots that generate dynamic, context-aware responses… However, to safeguard user experience, prevent potential misuse, and ensure alignment with our rules, **the deployment or operation of any AI reply bot requires prior written and explicit approval from X**." Source: https://help.x.com/en/rules-and-policies/x-automation (via secondary quotation).
- **How approval is sought:** Via your X point of contact or a request through the developer portal (as reported). Approval for an AI bot does not replace the opt-in rules (§11) or the Feb 2026 summoned-reply API restriction (§10). Source: https://help.x.com/en/rules-and-policies/x-automation (via secondary reporting).
- **AI-generated *posts* (not replies):** The Automation Rules are reported **not** to ban AI-written ordinary posts and never to use the phrase "AI-generated content" for ordinary posts; the rules that apply are the general automation rules (no spam, no duplicates across accounts, no unsolicited automated replies). The Create Post reference is reported to carry a `made_with_ai` field to disclose AI-generated **media**. Whether AI-generated *text* posts require disclosure was **not verified**. Source: https://help.x.com/en/rules-and-policies/x-automation ; https://docs.x.com/x-api/posts/create-post (via secondary reporting).
- **Pilot position:** Replies are out of scope. No AI reply bot will be deployed in the pilot, so no written approval is needed for the pilot. If replies are ever added, two gates apply in sequence: (1) X's prior written and explicit approval for the AI reply bot, and (2) the summoned-reply API restriction on self-serve. Do not build reply features on the assumption either gate will be granted, and do not use AI drafting of a reply as a workaround — a human sending an AI draft as a reply through the API is still an API reply subject to the summoned rule; whether a human-sent reply needs the AI-bot approval is an open question (see "Unverified").

## 13. Media upload restrictions

**Sources:** https://docs.x.com/x-api/media/introduction ; https://docs.x.com/x-api/media/quickstart/media-upload-chunked ; https://docs.x.com/x-api/media/quickstart/best-practices

**Verification status:** Not directly verified. Limits below are as reported from those official pages (Sep–Oct 2026); two secondary sources disagree on video limits (see note).

- **Flow (v2):** One-shot `POST /2/media/upload` for images and subtitle files; chunked flow for larger files/video: `POST /2/media/upload/initialize` → `POST /2/media/upload/{id}/append` (chunks ≤ 5 MB recommended, server maximum reported 8 MB, `segment_index` from 0) → `POST /2/media/upload/{id}/finalize` → `GET /2/media/upload?command=STATUS` until video processing succeeds. Scope `media.write` (OAuth 2.0) or OAuth 1.0a. The `media_id` is reported to live for 24 hours. Media bytes themselves are reported **not billed** (no "media upload" line on the rate card; nearest lines are Content: Manage / Media Metadata at $0.005 per request — whether those lines bill on upload calls was **not verified**). Source: https://docs.x.com/x-api/media/introduction ; https://docs.x.com/x-api/media/quickstart/media-upload-chunked (via secondary reporting).
- **Limits follow the authenticated X account's Premium/verified status, not the developer API plan.** Source: https://docs.x.com/x-api/media/introduction (via secondary reporting).
- **Reported limits (default account → Premium/verified):**
  - Image (`tweet_image` / `dm_image`): **5 MB**, JPG/PNG/GIF/WEBP. Animated GIF (`tweet_gif` / `dm_gif`): **15 MB**, ≤ 1280×1080, ≤ 350 frames, width × height × frames ≤ 300M pixels.
  - Video on a post (`tweet_video` / `amplify_video`): **20 min / 8 GB → 125 min / 16 GB**. Minimum duration 0.5 s everywhere.
  - Video in a DM (`dm_video`): **140 s / 512 MB → 10 min / 1 GB**.
  - Subtitle file: **1 MB**.
  - Per post: **up to 4 photos, or 1 GIF, or 1 video** (no mixing, as reported from best-practices; one source reports its own validator allows mixing — follow the official 4/1/1 rule).
  - Source: https://docs.x.com/x-api/media/introduction ; https://docs.x.com/x-api/media/quickstart/best-practices (via secondary reporting).
- **Conflicting report:** One implementation reference (griffinwork40/x-api-cli, verified Jul 2026 against official docs/OpenAPI) lists video at ≤ 512 MB / ≤ 140 s / ≤ 1920×1920 for `tweet_video`, and notes the byte limits were "corroborated, not on a live official page". That conflicts with the 20-min/8-GB figures above. The 20-min/8-GB figures are reported by multiple Sep–Oct 2026 sources citing the media introduction/best-practices pages and match X's consumer video limits, so they are the more likely current figures — but the conflict means **media limits must be re-checked directly** before promising video support.
- **Legacy v1.1:** `upload.x.com/1.1/media/upload.json` (INIT/APPEND/FINALIZE/STATUS, OAuth 1.0a only) was deprecated for self-serve 31 Mar 2025 (see §5). Use v2. Source: https://docs.x.com/x-api/media/introduction (via secondary reporting).

## 14. Attribution URL handling — does X pass UTM/signed links through unmodified?

**Sources:** https://docs.x.com/ (Counting Characters, under Fundamentals) ; https://help.x.com/ ("About X's link service (http://t.co)")

**Verification status:** Partially reported; **the signed-attribution-link question itself is NOT verified from any official source.**

- **t.co wrapping (reported from official docs):** "All URLs are wrapped with t.co shortener and count as 23 characters, regardless of the original length." Every URL posted is rewrapped in a t.co link before the post goes out; you cannot opt out ("X will shorten URLs you post on X. You cannot opt out of link shortening"). Shortening the link yourself first does not avoid it — X wraps the short link again and charges the same 23 characters. Source: https://docs.x.com/ (Counting Characters) ; https://help.x.com/ (About X's link service) (via secondary reporting).
- **UTM / query parameters:** Multiple secondary sources report that the t.co redirect delivers the reader to the **exact address posted, query string included**, so UTM parameters survive the wrapper and reach analytics intact ("the parameters ride along on the destination URL rather than being stored by X"). One low-quality source claims t.co *strips* UTM parameters — that claim is contradicted by the majority and by the mechanism described (t.co is a redirect to the full posted URL); treat survival as **reported, not directly verified**. No official statement specifically about UTM parameters was found. Source: https://help.x.com/ (About X's link service) (via secondary reporting).
- **Signed attribution links:** **No official X documentation was found stating how X treats signed/attribution links, whether a signature query parameter survives t.co wrapping, whether X strips or modifies any query parameter, or whether a signed link is treated differently from any other URL.** By the mechanism above, the full URL (including signature parameters) should be the redirect target, but that is inference from the general t.co behaviour, not a verified fact. **This is the pilot's most important open question** because the attribution link is the pilot's core feature: test with a real signed link on a test account and inspect the redirect target before promising attribution.
- **Link safety checks:** X checks every wrapped URL against a list of potentially dangerous sites; a reader hitting a flagged destination sees a warning screen first. Reasons given for t.co: character economy, click counting (described as "an important quality signal in determining how relevant and interesting each post is"), and dangerous-site checks. Source: https://help.x.com/ (About X's link service) (via secondary reporting).
- **Cost tied to links:** A post containing a URL bills at **$0.20** vs $0.015 for a plain post (§1). The charge is per post, not per link, and applies whether the URL is a plain link, a UTM-tagged link, or (presumably — unverified) a signed attribution link. No official exemption for attribution/owned links was found. Source: https://docs.x.com/x-api/getting-started/pricing (via secondary reporting).
- **Character budget:** Posts can contain up to 280 characters (Premium subscribers up to 25,000 characters, per X's Premium help page — not verified directly); every URL counts as 23 characters of that budget. Source: https://docs.x.com/ (Counting Characters) (via secondary reporting).

## 15. Data storage, retention, and redistribution requirements for data obtained via the API

**Sources:** https://docs.x.com/developer-terms (Developer Agreement, last updated 27 Apr 2026 as reported; Developer Policy; Restricted Use Rules)

**Verification status:** Not directly verified. Quotations below are from a secondary source that states it read all three documents in full in 2026, corroborated in part by a 2026-09-04 compliance summary. The Developer Policy and Restricted Use Rules are reported to **disagree** on one cap (see below) — that disagreement itself is unverified directly.

- **Licence:** The Developer Agreement (reported "Last Updated: April 27, 2026") grants a non-exclusive, royalty-free, non-transferable, non-sublicensable, **revocable** licence with four permitted uses. No selling, renting, sublicensing or redistributing except as expressly permitted. Source: https://docs.x.com/developer-terms (via secondary reporting).
- **Offline storage — keep it current / delete on change:** "If you store X Content offline, you must keep it up to date with the current state of that content on X. Specifically, you must delete or modify any content you have if it is deleted or modified on X. This must be done as soon as reasonably possible, or within 24 hours after receiving a request…" (quoted in secondary sources). Source: https://docs.x.com/developer-terms (Developer Policy, via secondary quotation).
- **Deletion deadlines (as tabulated in secondary sources):**
  - X requests deletion: **24 hours**.
  - User requests deletion: **24 hours**.
  - Content suspended/removed on X: **24 hours**.
  - API access terminated: delete **all** X data within **10 business days**, and you may have to prove it.
  - Source: https://docs.x.com/developer-terms (via secondary reporting).
- **Redistribution — IDs only:** "If you provide X Content to third parties, including downloadable datasets or via an API, you may only distribute Post IDs, Direct Message IDs, and/or User IDs (except as described below)." The recipient then rehydrates the IDs through the X API, so deleted posts do not live on in shared files. Source: https://docs.x.com/developer-terms (Developer Policy, via secondary quotation).
- **Redistribution caps (reported):**
  - Post IDs to any one entity: **1,500,000 per 30 days** without written permission.
  - Hydrated objects by manual download: **500 per person per day** (Developer Policy) **vs 50,000 per recipient per day** (Restricted Use Rules) — the two pages are reported to disagree; the correct figure is **unverified**. Do not rely on either without a direct read.
  - Source: https://docs.x.com/developer-terms (via secondary reporting).
- **Location data:** Store location data only together with the post it belongs to (not on its own). Source: https://docs.x.com/developer-terms (via secondary reporting).
- **No fixed retention period for stored X Content was found** (e.g. no "delete after 30 days" rule for ordinary stored posts). The operative duties are currency (sync deletions/modifications within 24 hours) and deletion on termination (10 business days). If a fixed retention limit exists elsewhere in the terms, it was not found in the sources consulted — **open question**.
- **Pilot implications:** Ventoryn's research reads (opportunity posts/users) are X Content. Store Post/User IDs + minimal fields, not full hydrated datasets; never redistribute hydrated post text to third parties; build a deletion-sync mechanism (re-check / honour deletion within 24 h) before storing research results long-term; delete all X data within 10 business days if API access ends. Drafts created by Ventoryn's agents are Ventoryn/customer content, not X Content, until posted — but once posted, the posted copy is X Content. Displaying X Content must use the API for the most current version and remove it promptly if deleted. The customer's own posted content shown back to that same customer inside Ventoryn is the lowest-risk display; showing other users' posts to the customer is where the redistribution/display rules bite.

---

## Configurable rate card

Prices are **USD**. "Verified" here means *reported by multiple independent checkers citing the official pricing page* — **no price in this table was directly read from docs.x.com in this session** (domain blocked). Treat every price as **REPORTED — RE-VERIFY IN THE LIVE CONSOLE** before quoting to a customer. Effective date 20 April 2026 is the reported date of the last price change; the pay-per-use model itself launched 6 February 2026.

| Operation | Reported price | Currency | Effective date (if stated) | Source URL |
|---|---|---|---|---|
| Post: Create (standard, no URL) | $0.015 per request | USD | 20 Apr 2026 (raised from $0.010) | https://docs.x.com/x-api/getting-started/pricing |
| Post: Create (with URL) | $0.200 per request | USD | 20 Apr 2026 | https://docs.x.com/x-api/getting-started/pricing |
| Post: Create (summoned reply) | $0.010 per request | USD | 20 Apr 2026 (unchanged) | https://docs.x.com/x-api/getting-started/pricing |
| Retrieved post / Post: Read | $0.005 per post returned | USD | — (pay-per-use from 6 Feb 2026) | https://docs.x.com/x-api/getting-started/pricing |
| Retrieved user / User: Read | $0.010 per user/resource | USD | — | https://docs.x.com/x-api/getting-started/pricing |
| Owned read (own app/account data, 12 endpoints) | $0.001 per resource | USD | 20 Apr 2026 | https://docs.x.com/x-api/getting-started/pricing |
| Following / Followers: Read | $0.010 per resource | USD | — | https://docs.x.com/x-api/getting-started/pricing |
| DM event read | $0.010 per event | USD | — | https://docs.x.com/x-api/getting-started/pricing |
| DM send / User interaction | $0.015 per request | USD | — | https://docs.x.com/x-api/getting-started/pricing |
| Counts (recent) | $0.005 per request | USD | — | https://docs.x.com/x-api/getting-started/pricing |
| Counts (all) / Trends | $0.010 per request | USD | — | https://docs.x.com/x-api/getting-started/pricing |
| Content: Manage / Media Metadata | $0.005 per request | USD | — | https://docs.x.com/x-api/getting-started/pricing |
| Media upload (bytes) | **UNVERIFIED** — reported as not billed; no "media upload" line on the rate card | USD | — | https://docs.x.com/x-api/getting-started/pricing |
| Interaction: Delete | $0.010 per request | USD | — | https://docs.x.com/x-api/getting-started/pricing |
| Monthly post-read cap (pay-per-use) | 3,000,000 post reads / billing cycle; Enterprise above | — | Reported rise from 2M to 3M in Oct 2026 — **UNVERIFIED directly** | https://docs.x.com/x-api/getting-started/pricing |
| Free tier / subscription | None for new developers | — | Free tier closed 6 Feb 2026 | https://docs.x.com/x-api/getting-started/pricing |
| One-time free credits | Up to $70 ($20 first card/purchase + up to $50 auto-recharge match) | USD | Rolling out since 2 Oct 2026 — **UNVERIFIED directly** | https://docs.x.com/x-api/getting-started/pricing ; https://docs.x.com/changelog |
| Enterprise | **UNVERIFIED** — no public price found; one source cites $42,000/mo starting point — **do not rely on it** | USD | — | https://docs.x.com/x-api/getting-started/pricing |

**Worked pilot costs (arithmetic from reported rates):** 30 approved link posts/mo = $6.00; 30 approved plain posts/mo = $0.45; research reading 15,000 posts/mo = $75.00; reading 1,000 user profiles/mo = $10.00. A single customer posting 1 link post/day and doing modest research is on the order of **$10–$85/mo in X API charges**, dominated by reads and by the $0.20 link charge — before any Ventoryn margin.

---

## Supported / Restricted / Unavailable — activity table for the pilot

| Activity | Status | Basis |
|---|---|---|
| Customer connects own X account via OAuth 2.0 + PKCE | **Supported** | https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code |
| Agent researches opportunities via recent search (`GET /2/tweets/search/recent`, 7 days) | **Supported** | https://docs.x.com/ (search reference) ; https://docs.x.com/x-api/getting-started/pricing ($0.005/post) |
| Agent reads posts by ID / users by username/ID | **Supported** | https://docs.x.com/ (API reference) |
| Agent drafts original posts; customer reviews + approves each in Ventoryn | **Supported** | https://help.x.com/en/rules-and-policies/x-automation (automated posts allowed; human approval exceeds the minimum) |
| Ventoryn publishes only the approved original post to the customer's own account (`POST /2/tweets`) | **Supported** | https://docs.x.com/x-api/posts/create-post ; https://help.x.com/en/rules-and-policies/x-automation |
| Post carries a signed attribution link (URL in post text) | **Supported, with cost + verification caveats** | Posting a URL is supported; it bills $0.20 (reported). Whether the signature survives t.co wrapping is **unverified** — see §14. https://docs.x.com/x-api/getting-started/pricing ; https://help.x.com/ (t.co) |
| Post carries an image/video via v2 media upload | **Supported** (with limits) | https://docs.x.com/x-api/media/introduction (5 MB image / 15 MB GIF / 20 min-8 GB video reported; conflicting video figure — re-check) |
| Scheduling a post for a future time via an X endpoint | **Unavailable** — no scheduling endpoint found; hold in Ventoryn and publish at the time | https://docs.x.com/ (Create Post reference; llms.txt review Oct 2026) |
| Replying to other users' posts via the API (unsummoned) | **Restricted / effectively unavailable on pay-per-use** | https://docs.x.com/changelog (23 Feb 2026) ; https://docs.x.com/x-api/posts/create-post — only summoned replies accepted (403 otherwise) |
| Replying to a commenter on the customer's own post | **Restricted — unverified edge** | Whether a comment summons you is not stated by X; test before relying on it. https://docs.x.com/x-api/posts/create-post |
| AI-generated replies (any AI reply bot) | **Restricted — prior written approval required** | https://help.x.com/en/rules-and-policies/x-automation ("AI-Powered Automated Replies") |
| Automated replies to keyword-search results | **Unavailable / prohibited** | https://help.x.com/en/rules-and-policies/x-automation |
| Unsolicited mentions / unsolicited DMs | **Unavailable / prohibited** | https://help.x.com/en/rules-and-policies/x-automation |
| Automated likes, follows, quote-posts | **Unavailable on pay-per-use** (endpoints removed Apr 2026) | https://docs.x.com/changelog ; https://docs.x.com/x-api/posts/create-post |
| Full-archive search (back to 2006) | **Restricted — access disputed** (access upgrade or Enterprise; not needed for pilot) | https://docs.x.com/ (search reference) |
| Storing researched X Content long-term | **Restricted** — keep current, delete/modify within 24 h of deletion/change; delete all within 10 business days of termination | https://docs.x.com/developer-terms (Developer Policy) |
| Redistributing hydrated X Content to third parties | **Restricted** — IDs only, 1.5M Post IDs / 30 days per entity; hydrated cap disputed (500 vs 50,000/day) | https://docs.x.com/developer-terms (Developer Policy / Restricted Use Rules) |
| Scripting x.com website instead of using the API | **Unavailable / prohibited** | https://help.x.com/en/rules-and-policies/x-automation |
| Multi-customer SaaS at scale on one pay-per-use app | **Restricted — scale gate** | https://docs.x.com/developer-terms (Developer Policy: Enterprise required beyond limited end-users / on written notice) |

---

## Unverified / open questions

Everything in this section needs a direct official read or a live test before the pilot is priced, built, or promised to a customer. **Nothing here was verified directly from an official X page in this session** — the domain block (docs.x.com, developer.x.com, help.x.com all blocked by policy for `browser.open`) is the root cause, and it applies to every item below, not just the ones listed.

1. **All five reference prices** ($0.015 / $0.20 / $0.005 / $0.010 / $0.010 summoned reply): convergent secondary reporting only. Re-open https://docs.x.com/x-api/getting-started/pricing in a live browser and confirm each line, the currency, the metering basis, and the effective date.
2. **3M vs 2M monthly post-read cap:** sources disagree by check date (2M earlier in 2026, 3M from Oct 2026). Confirm the current cap and whether it is per billing cycle or calendar month.
3. **$0.20 charge for a signed attribution link specifically:** no official statement found that an attribution/signed link is treated any differently from any other URL, or that it is exempt. Confirm in the console and by a live test post.
4. **Does a signed/UTM link survive t.co wrapping unmodified?** No official statement found. Live-test a signed link and inspect the redirect target and the signature validation at the destination.
5. **Whether the Feb 2026 summoned-reply rule is still current on the live Manage Posts page:** last secondary confirmation Oct–Nov 2026; confirm at https://docs.x.com/x-api/posts/create-post and https://docs.x.com/changelog.
6. **Whether a comment on your own post counts as a summon:** X is reported not to state this. Live-test required.
7. **Whether a human sending an AI-drafted reply needs the AI-reply-bot written approval:** the policy speaks of "deployment or operation of any AI reply bot"; a human-in-the-loop reply flow is not expressly addressed. Confirm with X before adding any reply feature.
8. **Whether AI-generated *text* posts require disclosure:** the `made_with_ai` field is reported for AI-generated *media*; no rule found for AI-drafted text. Confirm at https://docs.x.com/x-api/posts/create-post.
9. **Full-archive search access on pay-per-use:** is it available with a self-serve access upgrade, or Enterprise-only? Sources conflict. Not needed for the pilot, but confirm before promising any backfill.
10. **Enterprise price / threshold:** no public price found; one source's $42,000/mo figure is unverified. The Developer Policy's "greatly exceeds normal levels" multi-user threshold is unquantified. Confirm what pilot-scale multi-customer use triggers Enterprise, and the process/timeline for that decision.
11. **Refresh-token lifetime:** no fixed duration found (reported only as "long-lived / until revoke or rotate fail"). Confirm whether refresh tokens expire after a fixed period.
12. **Single live OAuth grant per (app, user):** reported for the Ads MCP surface; unknown whether it applies to core API v2 OAuth 2.0. Live-test re-connection from a second device.
13. **OAuth 1.0a status:** conflicting reports (still accepted vs removed for most writes vs retiring with a token-exchange grant from 21 Sep 2026). Irrelevant if the pilot uses OAuth 2.0 + PKCE only, but confirm before touching 1.0a.
14. **Media limits conflict:** video reported as both 20 min/8 GB (multiple Sep–Oct 2026 sources) and ≤ 512 MB/140 s (one Jul 2026 implementation reference). Re-check https://docs.x.com/x-api/media/introduction directly before promising video.
15. **Media upload billing:** reported as not billed, with Content: Manage / Media Metadata at $0.005 possibly applying — confirm whether upload calls bill.
16. **Hydrated-content redistribution cap:** Developer Policy (500/person/day) vs Restricted Use Rules (50,000/recipient/day) disagree in secondary reporting. Direct-read both pages at https://docs.x.com/developer-terms.
17. **Fixed retention period for stored X Content:** none found; confirm there is no separate retention limit in the full Developer Agreement/Policy text.
18. **Account-level daily post limits:** 50 original posts/day (unverified accounts, May 2026) vs 2,400/day (general cap) appear contradictory. Confirm at https://help.x.com/ (About X limits) for the customer account type in the pilot.
19. **Per-app `POST /2/tweets` 10,000/24 h limit — shared across all customers of one app?** One source disputes this. Confirm in the console / rate-limit docs; this caps total pilot throughput per app.
20. **Use-case application timeline:** no timeline found for developer-account approval (how long, what is asked, rejection/re-review behaviour). Confirm during the actual application.
21. **$70 one-time free credits:** eligibility and availability (reported rollout from 2 Oct 2026) not directly verified.
22. **Direct official reads still outstanding for every section:** the pricing page, Getting Access, OAuth 2.0 PKCE guide, Create Post, rate limits, media guides, Automation Rules, and Developer Terms were all cited second-hand in this document. A live-browser pass over those eight URLs is the required next step; do not present this document to a customer as "verified against X's docs" until that pass is done.

---

## Sources consulted (method note)

- Official URLs supplied in the task (not directly opened — domain blocked): https://docs.x.com/ , https://developer.x.com/ , https://help.x.com/en/rules-and-policies/x-automation
- Web search (2026-11-08) over official-doc citations and independent checkers that state they read docs.x.com / help.x.com in Sep–Nov 2026, including postzen.dev, superframeworks.com, imisofts.com, twitterapi.io, outstand.so, xautopilot.app, trackmedia.app, soloop.io, kairopost.com, opentweet.io, bulkpublish.com, and GitHub compliance summaries (xbox002000/x-world, griffinwork40/x-api-cli) citing specific docs.x.com paths. Secondary sources are *not* cited as authorities in the body above; the official page URL is cited per claim, with the verification status stated explicitly wherever the official page was not directly read — which, in this session, is everywhere.
- No copyrighted text was copied at length; policy wording is paraphrased except for short quoted sentences identified as quotations in §§3, 10, 12, 15.
