# Facebook (Meta) API Verification — Ventoryn First-Customer Program, Phase 0

**Date:** 2026-11-08
**Scope:** Inspection only. No code changes, no spending, no credentials, no live actions.
**Use case verified (only):** A SaaS customer connects **their own Facebook Page**. Ventoryn's agents research opportunities and draft **original** promotional/informational posts. The customer reviews and approves each post inside Ventoryn. Ventoryn publishes **only the approved original post** to that customer's own Page, with a signed attribution link. **No** automated replies, **no** unsolicited mentions, **no** unsolicited DMs, **no** artificial engagement, **no** mass posting. Reading comments/mentions on the customer's own Page is a secondary interest.

## How this document was verified

- **Directly fetched official Meta documentation (primary evidence):** three pages on `developers.facebook.com` were read in full on 2026-11-08:
  - Facebook Pages API overview — https://developers.facebook.com/documentation/pages-api (updated May 7, 2026)
  - Get started with the Pages API — https://developers.facebook.com/documentation/pages-api/getting-started (updated Jun 30, 2026)
  - Access Tokens for Meta Technologies — https://developers.facebook.com/documentation/facebook-login/guides/access-tokens (updated Nov 25, 2025)
- **Access limitation encountered:** several other official pages returned HTTP 403 to the fetch tool in this session and could not be read directly, including the Permissions Reference, App Review guide, Business Verification guide, Graph API Rate Limiting overview, Pages API Posts-and-Comments/Webhooks sub-pages, the Page Photos reference, and the Platform Terms page. Per the task rules, those requests were **not** retried. Anything that rests only on those pages is marked **NOT DIRECTLY VERIFIED** below, even where a secondary source quotes the official text. Where a secondary source reproduces official wording, that is noted as "quoted in a secondary source" — it is **not** treated as verified.
- **Rule applied throughout:** every claim carries its source URL. Where no official source could be read, the document says so explicitly instead of inventing a price, timeline, or rule.

## Verdict

**The core pilot use case is supported by Meta's platform, and the API itself carries no per-call price — but it is gated, not open.** Ventoryn must build a Business-type Meta app, complete Business Verification, and pass per-permission App Review for Advanced Access before **any** customer outside Ventoryn's own team can connect a Page. That approval path is the real cost: calendar time, a working product demo (screencast), a privacy policy, and a legal business entity — not money paid to Meta. Publishing itself is a single documented call (`POST /{page-id}/feed` with `pages_manage_posts` and a Page access token), the connecting user must hold the `CREATE_CONTENT` task on the Page, and Page tokens obtained from a long-lived user token do not expire on a timer (though Meta warns lifetimes can change without notice and tokens are invalidated by password changes, de-authorisation, or loss of Page role). Comment **reading** and replying sit under a separate permission (`pages_manage_engagement`) that the pilot does not need for posting-only operation and should not request. **Nothing in the pilot is blocked outright by the platform** — the blockers are procedural (verification + review) and must be sequenced before the first external customer connects. Several details (exact rate-limit numbers, App Review timelines, media size limits, Platform Terms data-retention wording, and whether Tech Provider verification applies to Ventoryn) could **not** be verified from an official page in this session and are listed as open questions in the final section.

---

## 1. Pricing

**Verified facts**

- Meta's Pages API documentation describes the API as a set of endpoints, tokens, and webhooks for managing Pages. It contains **no price, no price list, and no per-call charge** anywhere in the pages read.
  - Source: https://developers.facebook.com/documentation/pages-api
  - Source: https://developers.facebook.com/documentation/pages-api/getting-started
- The Access Tokens guide likewise contains no pricing information.
  - Source: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens

**Not directly verified**

- **Whether Graph API / Pages API calls are free:** NO official pricing page could be read in this session (no Meta pricing page for the Graph/Pages API was found among the official docs fetched; the Platform Terms page, which third-party guides cite for the "no guarantee the platform will always be free" wording, returned 403). The consistent secondary reporting is that Meta charges nothing per Graph API or Pages API call and that the only Meta API with direct per-message billing is WhatsApp — but that is **not officially verified here** and must be confirmed from an official source before it is stated to a customer.
- **What, if anything, is metered or charged:** not verifiable from the official pages read. No metering, quota pricing, or paid tier for the Pages API appears in the three pages fetched. Any statement that "the API is free" should be treated as **unverified** until an official source is read (recommended: the Platform Terms at https://developers.facebook.com/terms and any pricing statement in the App Dashboard, read in a live browser session).
- No cost was found for creating a Meta app, requesting App Review, or completing Business Verification in the pages read; equally, no official page read states those steps are free. Do not quote a $0 cost to customers on this evidence alone.

## 2. App type and setup

**Verified facts**

- The Pages API is used by creating a Meta app and calling it with a Page access token; the Pages API overview lists app creation as the starting point ("Create an app — Create a Meta app with the Pages API use case").
  - Source: https://developers.facebook.com/documentation/pages-api
- The getting-started guide requires a Facebook Page, a Page access token, and four permissions (`pages_manage_metadata`, `pages_manage_posts`, `pages_manage_read_engagement` [sic — see note below], `pages_show_list`) before the first post can be made.
  - Source: https://developers.facebook.com/documentation/pages-api/getting-started
- **Documentation inconsistency flagged:** the getting-started page lists a permission spelled `pages_manage_read_engagement`. No such permission exists in the Pages API overview, which lists `pages_read_engagement` instead. This appears to be a typo in Meta's own getting-started page. Ventoryn should request `pages_read_engagement` (the name used everywhere else in Meta's docs) and confirm the spelling in the App Dashboard before submitting for review.
  - Source: https://developers.facebook.com/documentation/pages-api/getting-started and https://developers.facebook.com/documentation/pages-api

**Not directly verified**

- **Business app type:** the claim that the app must be of type **Business** (and that the type cannot be changed later) could not be verified from an official page read in this session — the app-creation guide and app-type documentation returned 403 / were not reachable. Secondary sources agree unanimously that a Business-type app is required for the `pages_*` permissions, but treat this as **unverified** until the official "Create an app" documentation is read.
- **Business Verification requirements (documents, process, timeline):** not directly verified. The official Business Verification guide could not be fetched. A secondary source (an architecture decision record) quotes the official Business Verification page as stating: *"Apps that request advanced access for permissions and apps that allow other Businesses to access their own data must be connected to a Business that has completed Business Verification"* — with the exception *"If your app will only be used by app users who have a role on the app itself you do not need to complete verification."* That quotation is **not** treated as verified here; it is included only as a pointer for the follow-up check. The implication, if the quotation is accurate, is that Ventoryn **must** complete Business Verification because its customers are not app-role holders.
- **App Review process and timelines:** the Pages API overview confirms App Review is required for extended permissions and describes the submission as: request the permissions in the Developer dashboard, provide detailed use cases and screencast videos, submit, and respond to feedback.
  - Source: https://developers.facebook.com/documentation/pages-api
  - Timelines (e.g. "up to 10 business days" or "1–3 weeks" cited by secondary sources) are **not verified** from any official page read.
- **Standard vs Advanced Access:** the distinction (Standard = only app-role users; Advanced = any user, requires Business Verification + App Review per permission) could not be verified from an official page read in this session — the Access Levels / Permissions Reference pages returned 403. It is consistent across secondary sources but is **unverified** here.
- **Annual Data Use Checkup:** not verified from an official page read in this session (secondary sources report it is required to keep Advanced Access). Open question.
- **Tech Provider verification:** whether a SaaS that serves other businesses' Pages must additionally complete Tech Provider / Access Verification is **not verified** from an official page read. Secondary sources disagree on whether it applies to a Pages-only SaaS (it is clearly documented for WhatsApp/Marketing-API tech providers). This is a material open question for Ventoryn — see §11.

## 3. The three permissions: `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`

**Verified facts (from the official Pages API overview)**

| Permission | What Meta's Pages API overview says it allows |
|---|---|
| `pages_show_list` | "Show Pages managed by a user" |
| `pages_read_engagement` | "Read content posted to the Page" |
| `pages_manage_posts` | "Publish and schedule content" |

- Source: https://developers.facebook.com/documentation/pages-api
- Posting specifically: "Create new posts on a Page. Request: `POST /{page-id}/feed`. Parameters: `message`, `link`, `picture`, `published`. Permissions: `pages_manage_posts`."
  - Source: https://developers.facebook.com/documentation/pages-api
- The overview also lists the neighbouring permissions relevant to the secondary listening interest: `pages_manage_engagement` ("Moderate comments, delete posts"), `pages_read_user_content` ("Read user-generated content on the Page"), `pages_manage_metadata` ("Manage settings for the Page"), `pages_messaging` ("Manage and send messages on behalf of the Page"), and `business_management` ("Manage business assets related to the Page").
  - Source: https://developers.facebook.com/documentation/pages-api
- **Comment reading/replying is a separate permission:** the overview states "Read, create, and moderate comments on Page posts. Read comments: `GET /{object-id}/comments`, Post a comment: `POST /{object-id}/comments`, Delete a comment: `DELETE /<COMMENT_ID>`. Permissions: `pages_manage_engagement`." So reading comments on the Page's posts is grouped under `pages_manage_engagement`, **not** under `pages_read_engagement`, in Meta's own overview.
  - Source: https://developers.facebook.com/documentation/pages-api
- **Mentions:** "Retrieve posts or comments where the Page is mentioned. Request: `GET /{page-id}/tagged`. Permissions: `pages_read_user_content`."
  - Source: https://developers.facebook.com/documentation/pages-api

**Not directly verified**

- **Which access level (Standard vs Advanced) each of the three permissions needs for a SaaS posting to a customer's Page:** not verifiable from the official pages read — the Permissions Reference page (which lists per-permission access levels and review evidence) returned 403. Secondary sources are consistent that all three need Advanced Access via App Review to operate on Pages belonging to people without a role on the app, and that `pages_manage_posts` additionally depends on `pages_read_engagement` and `pages_show_list` being requested together — but both points are **unverified** here.
- **What review evidence Meta asks for per permission** (screencast contents, test credentials, data-handling questionnaire, "at least one successful API call within 30 days of submission"): the Pages API overview confirms only that App Review requires "detailed use cases and screencast videos" and responding to feedback. The per-permission evidence checklist is **not verified** from an official page read.
  - Source for the confirmed part: https://developers.facebook.com/documentation/pages-api
- **Whether `pages_read_engagement` alone suffices to read the Page's own posts and insights:** the overview ties `GET /{page-id}/insights` to `pages_read_engagement` and describes it as "Read content posted to the Page", which supports the pilot's read-back use case; comment reading, however, is under `pages_manage_engagement` (see above).
  - Source: https://developers.facebook.com/documentation/pages-api

## 4. Page access token behavior

**Verified facts (from the official Access Tokens guide)**

- "Page access tokens let you read, write, and modify data belonging to a Facebook Page. To obtain one, first get a user access token, then exchange it for a Page access token via the Graph API."
  - Source: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens
- The exchange call is `GET /{your-user-id}/accounts` with a user access token; it "returns a list of Pages you have a role on, including the Page category, your permissions on each Page, and the Page access token." The response includes a `tasks` array per Page (e.g. `ANALYZE`, `ADVERTISE`, `MODERATE`, `CREATE_CONTENT`, `MANAGE`).
  - Source: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens
- "With a Page access token, you can make API calls on behalf of a Page, such as posting a status update to a Page or reading Page Insights data." "Page access tokens are unique to each Page, admin, and app combination."
  - Source: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens
- **Lifetimes:** "Access tokens come in two forms: short-lived and long-lived. Short-lived tokens typically last about one to two hours, while long-lived tokens last about 60 days. **Do not depend on these lifetimes remaining the same — they may change without warning or expire early.**"
  - Source: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens
- The getting-started guide confirms that the token returned by `/user_id/accounts` is "a short-lived Page access token".
  - Source: https://developers.facebook.com/documentation/pages-api/getting-started
- **Long-lived user tokens:** "Access tokens generated via web login are short-lived, but you can convert them to long-lived tokens by making a server-side API call with your app secret."
  - Source: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens
- **System User tokens (server automation without re-authentication):** "System User access tokens let your app perform programmatic, automated actions on Ad objects or Pages without requiring input from an app user or re-authentication." They are generated in Business Settings → System Users, with an admin or employee role, asset assignment, and a chosen token expiration preference. "Apps with Standard access to the Marketing API receive long-lived tokens that do not expire based on time, though they are still subject to invalidation for other reasons. This also applies to access tokens for System Users in Business Manager."
  - Source: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens
- **Storage:** "The length of access tokens changes over time as Meta updates what they store and how they are encoded. Use a variable-length data type without a specific maximum size to store access tokens." Tokens must be transferred over HTTPS.
  - Source: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens

**Not directly verified**

- **The "non-expiring Page token" rule:** the widely reported flow (short-lived user token → exchange for a ~60-day long-lived user token via `GET /oauth/access_token?grant_type=fb_exchange_token` with app ID + app secret → `GET /me/accounts` with that token returns Page tokens that never expire) could **not** be verified from the official pages read. The Access Tokens guide read here describes the user-token exchange and the Page-token exchange, and notes some tokens "do not expire based on time", but it does **not** state in the text fetched that Page tokens derived from a long-lived user token never expire. Secondary sources agree on the flow, but the exact non-expiry statement is **unverified** and must be confirmed in the official guide (or in the Access Token Debugger) before Ventoryn designs around "store once, never refresh".
- **Refresh/renewal rules and invalidation triggers:** the official guide warns lifetimes "may change without warning or expire early" and advises handling errors; specific invalidation triggers (password change, app de-authorisation, loss of Page role) are reported by secondary sources but are **not verified** from an official page read. Ventoryn should in any case build token validation (`debug_token`) and a re-connect flow rather than assuming permanence.
  - Source for the warning: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens

## 5. Page roles / tasks — must the customer hold CREATE_CONTENT?

**Verified facts**

- The Pages API getting-started guide states the prerequisite as: "A Facebook Page, this can be an unpublished or published Page **on which you can perform the `CREATE_CONTENT` task**."
  - Source: https://developers.facebook.com/documentation/pages-api/getting-started
- The Access Tokens guide's example `/accounts` response shows the `tasks` array on each Page, including `CREATE_CONTENT` alongside `ANALYZE`, `ADVERTISE`, `MODERATE`, and `MANAGE`, confirming that tasks are per-user, per-Page, and returned with the token.
  - Source: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens
- **Interpretation (grounded in the two sources above):** the person who connects the Page to Ventoryn must hold the `CREATE_CONTENT` task on that Page; the Page token Ventoryn stores carries the tasks of that connecting user. If the connecting user loses the task, the token's authority goes with it (the token is "unique to each Page, admin, and app combination").

**Not directly verified**

- **How Page roles/tasks are granted and managed** (new Pages experience: full control / partial access, task assignment in Page Settings or Business Settings, which task set maps to the old admin/editor roles): not covered in the official pages read. The task names themselves are verified (see above); the management UI and whether a non-admin with only `CREATE_CONTENT` can connect are **unverified**.
- **Whether `MODERATE` is required to reply to comments:** reported by secondary sources (commenting requires the `MODERATE` task) but **not verified** from an official page read.
- **Whether a Page admin is required vs any task holder:** the getting-started guide requires only the `CREATE_CONTENT` task, not admin status — that is the verified statement. Whether Meta's App Review expects an admin for the demo is unverified.
  - Source: https://developers.facebook.com/documentation/pages-api/getting-started

## 6. Rate limits

**Verified facts**

- The Pages API overview lists "rate limits" among common errors, alongside invalid tokens and missing permissions, and advises caching responses and handling paging.
  - Source: https://developers.facebook.com/documentation/pages-api
- No numeric limit appears in any official page read in this session.

**Not directly verified**

- **All numeric rate limits are unverified.** The official Graph API Rate Limiting overview returned 403 and could not be read. The formulas consistently reported by secondary sources — Platform limits of 200 × number of app users per rolling hour for app/user tokens, and Business Use Case (BUC) limits of 4,800 × number of engaged users per 24 hours for Page/system-user tokens, with `X-App-Usage` and `X-Business-Use-Case-Usage` headers and error codes 4, 32, and 80001 — are **not verified** from an official page in this session. A GitHub-hosted mirror of Meta's own rate-limiting page text reproduces those formulas, but a mirror is not an official source.
- **Practical implication for the pilot (reasoning, not a verified fact):** a pilot posting a handful of approved posts per customer per day is orders of magnitude below any reported limit; rate limits are therefore unlikely to be the binding constraint, but the numbers must be confirmed from the official page (https://developers.facebook.com/docs/graph-api/overview/rate-limiting/) before any capacity claim is made.

## 7. Comment and mention handling

**Verified facts (from the official Pages API overview)**

- **Reading comments:** `GET /{object-id}/comments`. **Posting a comment (reply):** `POST /{object-id}/comments`. **Deleting a comment:** `DELETE /<COMMENT_ID>`. All three are listed under permission `pages_manage_engagement`.
  - Source: https://developers.facebook.com/documentation/pages-api
- **Mentions:** `GET /{page-id}/tagged` returns posts or comments where the Page is mentioned, under permission `pages_read_user_content`.
  - Source: https://developers.facebook.com/documentation/pages-api
- **Webhooks:** "Webhooks provide real-time updates for changes or events on the Page, such as new comments, likes, or messages. 1. Configure a callback URL in the developer dashboard. 2. Subscribe to desired fields (e.g., `feed`, `mentions`, `messages`). 3. Your service will receive HTTP POST notifications for relevant events."
  - Source: https://developers.facebook.com/documentation/pages-api
- **Reading the Page's own feed/posts:** `GET /{page-id}/feed` returns the Page's posts (used in the getting-started guide to verify a published post).
  - Source: https://developers.facebook.com/documentation/pages-api/getting-started

**Not directly verified**

- **Webhook payload contents** (what exactly a `feed` or `mentions` event delivers — comment text, author, IDs, and what is withheld for privacy): the Webhooks sub-page returned 403 and could not be read. Secondary sources report that `feed` covers new posts, comments, and reactions and that some mention data may not be queryable due to privacy restrictions — **unverified**.
- **Whether a webhook subscription requires `pages_manage_metadata`** (to call `/{page-id}/subscribed_apps`) and per-Page app installation: reported by secondary sources, **not verified** from an official page read.
- **Replying to comments as the Page vs as the app:** the endpoint exists (`POST /{object-id}/comments` with `pages_manage_engagement`); whether replies appear in the Page's voice and any restrictions on automated replies are **not verified** from an official page read. Note: Ventoryn's pilot scope excludes automated replies entirely, so this affects only the secondary listening interest.
- **Whether webhooks fire for comments on ads:** reported as excluded by secondary sources; **unverified**.

## 8. Media rules for Page posts

**Verified facts**

- The Pages API overview confirms posts can carry a `picture` parameter and that `pages_manage_posts` covers "Page posts, photos, and videos" (via the permission's described scope "Publish and schedule content" and the posting endpoint parameters `message`, `link`, `picture`, `published`).
  - Source: https://developers.facebook.com/documentation/pages-api
- No sizes, formats, resolutions, or duration limits appear in the official pages read in this session.

**Not directly verified**

- **All numeric media limits are unverified.** The Page Photos reference, Page Videos reference, and Reels publishing reference all returned 403 / were not readable. Secondary sources citing those references report: photos max 10 MB, accepted types JPEG/BMP/PNG/GIF/TIFF, PNG under 1 MB recommended to avoid pixelation; videos up to 4 GB via the Resumable Upload API; Reels 3–90 seconds, 9:16, 1080×1920 recommended (540×960 minimum), 24–60 fps, max 30 Reels/day; no published character limit for post text. **None of this is verified** from an official page in this session and must be confirmed from the official references before the pilot accepts customer media.
- **Scheduling window** for `scheduled_publish_time` (reported as 10 minutes to 30 days, or 10 minutes to 75 days depending on post type): **unverified**.
- **Whether link posts render a preview from the linked URL and any requirements on the linked page (e.g. reachable by Meta's scraper, og:image):** not covered in the pages read; **unverified**.

## 9. Ventoryn-build vs customer-does split

This split is **derived** from the verified facts above (what the docs require) plus the stated use case. Items marked with an asterisk (*) rest on an unverified premise (Business-type app, Advanced Access) and should be re-confirmed once those premises are verified.

| Responsibility | Owner | Basis |
|---|---|---|
| Create the Meta developer account and the Meta app (Business type*) | Ventoryn | Pages API overview: "Create an app — Create a Meta app with the Pages API use case" — https://developers.facebook.com/documentation/pages-api |
| Add privacy policy URL, app icon, category, OAuth redirect URI | Ventoryn | Required for App Review per the overview's submission description; the specific fields are reported by secondary sources, not verified in the pages read |
| Complete Business Verification* | Ventoryn (legal entity, documents) | App Review prerequisite per overview; Business Verification itself unverified — see §2 |
| Submit each permission for App Review with use-case descriptions and screencast videos; respond to reviewer feedback* | Ventoryn | https://developers.facebook.com/documentation/pages-api |
| Build the Facebook Login / OAuth connect flow requesting `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` (and `pages_manage_engagement` / `pages_read_user_content` only if listening is added later) | Ventoryn | https://developers.facebook.com/documentation/pages-api |
| Exchange the user token for a Page token, store it encrypted (variable-length storage), validate it, and handle expiry/invalidation + re-connect | Ventoryn | https://developers.facebook.com/documentation/facebook-login/guides/access-tokens |
| Build the in-Ventoryn review/approval gate and publish only approved posts via `POST /{page-id}/feed` | Ventoryn | https://developers.facebook.com/documentation/pages-api |
| Build webhook endpoint + Page subscription if comment/mention listening is added | Ventoryn | https://developers.facebook.com/documentation/pages-api |
| Own a Facebook Page | Customer | https://developers.facebook.com/documentation/pages-api/getting-started |
| Hold the `CREATE_CONTENT` task on that Page (granted by the Page's admin) | Customer (the connecting person) | https://developers.facebook.com/documentation/pages-api/getting-started |
| Click through Ventoryn's connect flow, authorise the app, and pick the Page | Customer | https://developers.facebook.com/documentation/pages-api (flow: app requests permissions → user authorises → app exchanges code for user token → app requests Page token) |
| Review and approve each draft inside Ventoryn before anything publishes | Customer | Use-case requirement (not a Meta rule); Meta's side only requires the token and permission |
| Keep their Page role/task; re-connect if they change their Facebook password, remove the app, or lose the role | Customer (with Ventoryn prompting) | Token invalidation behaviour is unverified — see §4 |
| Nothing the customer must do in Meta's consoles for the pilot | — | On the verified flow, the customer never touches the App Dashboard, Business Verification, or App Review; those are Ventoryn's. (If Tech Provider verification or a Business Asset connection is required, this may change — see §11.) |

## 10. Platform Policy constraints relevant to a SaaS posting on behalf of customers

**Verified facts**

- The Pages API overview's best-practice list states: "Use the minimum permissions needed. Cache responses where possible. Handle paging for large result sets. Respect user privacy and Facebook policies."
  - Source: https://developers.facebook.com/documentation/pages-api
- The overview also states that extended permissions require App Review — i.e. Meta reviews the *intended use* of each permission before it may be exercised on other people's Pages.
  - Source: https://developers.facebook.com/documentation/pages-api
- The Access Tokens guide requires tokens to be transferred only over HTTPS, never hard-coded into client-side code or app binaries, and the app secret to be used only in server-side code ("Never share your app secret with anyone").
  - Source: https://developers.facebook.com/documentation/facebook-login/guides/access-tokens

**Not directly verified — Platform Terms / Developer Policies**

- The Platform Terms page (https://developers.facebook.com/terms) returned 403 and could not be read. The following are therefore **not verified**, although they are quoted from a publicly accessible mirror of the official terms text and are included here only as pointers for the follow-up read:
  - That the legacy Facebook Platform Policy was replaced by the Platform Terms, and references to "Platform Policy" now mean those Terms.
  - Prohibited practices including: processing Platform Data to discriminate; to make eligibility determinations (housing, employment, insurance, credit, etc.); to perform/facilitate surveillance; selling, licensing, or purchasing Platform Data; processing Platform Data without valid user consent to build or augment user profiles; and attempting to decode/re-identify/anonymise-reverse Platform Data.
  - Data retention/deletion duties: update or delete Platform Data promptly on request from Meta or the user; give users an easily accessible, clearly marked way to ask for their Platform Data to be modified or deleted; delete all Platform Data as soon as reasonably possible when it is no longer necessary for a legitimate business purpose (and on other specified events).
- **Automated posting:** no official page read in this session states a rule that prohibits or permits automated/scheduled posting to a Page via the API. The Pages API itself is designed for programmatic posting (including a `published`/`scheduled_publish_time` mechanism), and Ventoryn's model — human-approved, original content, posted to the customer's own Page — does not match any prohibited practice found in the material read. A specific "no artificial engagement / no inauthentic behaviour" rule was **not** located in an official page read; it must be checked in the Platform Terms and Developer Policies before launch. The pilot's exclusions (no automated replies, no unsolicited mentions/DMs, no artificial engagement, no mass posting) are consistent with the least-privilege and privacy guidance that *was* verified.
- **Attribution links:** no official rule about attribution links, "signed" links, or link-in-post-text requirements was found in the pages read. A link in a post is carried by the `link` parameter of `POST /{page-id}/feed`; whether Meta treats a tracking/attribution URL differently (e.g. scraper reachability, preview rendering) is **unverified** — see §8.
- **Data use/retention:** beyond the unverified Terms quotations above, the only verified guidance is "Respect user privacy" and minimum-permission use. Ventoryn's data-handling design (what comment/engagement data it stores, for how long, and the user's deletion route) must be settled against the Platform Terms once they are read — see §11.
- **No scraping:** the pilot uses only the API; no official anti-scraping rule was read in this session, but the use case does not involve scraping.

## Supported / Restricted / Unavailable — pilot activity table

"Supported" means a documented API capability confirmed in an official page read. "Restricted" means possible only with an additional permission, review step, or condition. "Unavailable / not verified" means no official page read confirms it.

| Activity | Status | Evidence / note |
|---|---|---|
| Customer connects their own Page via OAuth | **Supported** (after Advanced Access*) | Flow documented: app requests permissions → user authorises → code exchanged for user token → Page token requested. https://developers.facebook.com/documentation/pages-api |
| List the Pages the customer manages so they can pick one | **Supported** | `pages_show_list` — https://developers.facebook.com/documentation/pages-api |
| Publish an approved original text post to the customer's Page | **Supported** (after Advanced Access*) | `POST /{page-id}/feed` + `pages_manage_posts` — https://developers.facebook.com/documentation/pages-api |
| Publish a link post (message + link, incl. attribution link) | **Supported** (after Advanced Access*) | `link` parameter on `POST /{page-id}/feed` — https://developers.facebook.com/documentation/pages-api. Scraper/preview behaviour unverified. |
| Publish a photo with a post | **Supported** (after Advanced Access*) | `picture` parameter; `pages_manage_posts` — https://developers.facebook.com/documentation/pages-api. Size/format limits unverified. |
| Schedule a post for later | **Supported** (after Advanced Access*) | `published` parameter; overview lists "Publish and schedule content" for `pages_manage_posts` — https://developers.facebook.com/documentation/pages-api. Scheduling window unverified. |
| Read back the Page's own posts to confirm publication | **Supported** | `GET /{page-id}/feed`; `pages_read_engagement` — https://developers.facebook.com/documentation/pages-api/getting-started and https://developers.facebook.com/documentation/pages-api |
| Read Page insights (impressions, fans) | **Supported** (after Advanced Access*) | `GET /{page-id}/insights` + `pages_read_engagement` — https://developers.facebook.com/documentation/pages-api |
| Read comments on the Page's posts | **Restricted** — needs `pages_manage_engagement` (separate App Review item) | `GET /{object-id}/comments` + `pages_manage_engagement` — https://developers.facebook.com/documentation/pages-api |
| Reply to a comment | **Restricted** — needs `pages_manage_engagement`; **excluded from pilot scope** (no automated replies) | `POST /{object-id}/comments` — https://developers.facebook.com/documentation/pages-api |
| Receive new comments/mentions in real time via webhooks | **Restricted** — needs webhook setup + Page subscription; payload contents unverified | `feed`, `mentions`, `messages` fields — https://developers.facebook.com/documentation/pages-api |
| Read posts/comments where the Page is mentioned | **Restricted** — needs `pages_read_user_content` (separate App Review item) | `GET /{page-id}/tagged` — https://developers.facebook.com/documentation/pages-api |
| Send a DM / Messenger message on the Page's behalf | **Restricted** — needs `pages_messaging`; **excluded from pilot scope** | Listed as a permission in the overview — https://developers.facebook.com/documentation/pages-api |
| Post to a personal profile | **Unavailable** | No endpoint for it in the Pages API, which is Page-only by design. (The 2018 removal of profile posting is reported by secondary sources; the Pages API pages read here simply contain no profile-posting capability.) |
| Post to a Facebook Group | **Unavailable** | No Group capability in the Pages API pages read. (Groups API removal reported by secondary sources; not verified here.) |
| Unsolicited mentions of other users/Pages, mass posting, artificial engagement | **Excluded by design / not verified as permitted** | No official rule located in the pages read; pilot excludes these regardless. Must be checked against Platform Terms. |
| Non-expiring Page token (store once, never refresh) | **Not verified** | The exchange flow is documented, but the "never expires" statement could not be verified from an official page read — see §4. Build validation + re-connect regardless. |

\* "After Advanced Access" rests on the unverified Standard/Advanced premise (§2). If that premise is confirmed, all customer-facing publishing requires Business Verification + per-permission App Review first.

## Unverified / open questions

These items could **not** be verified from an official Meta page in this session (the relevant official pages returned HTTP 403 to the fetch tool and were not retried, per task rules). Do not treat any of them as settled.

1. **Pricing:** no official Meta pricing statement for Graph/Pages API calls was read. Is there genuinely no per-call charge, and does the Platform Terms "no guarantee the platform will always be free" wording apply? Read https://developers.facebook.com/terms and the App Dashboard in a live browser session.
2. **Business-type app requirement** (and that the type is immutable after creation). Read the official "Create an app" documentation.
3. **Business Verification:** exact documents accepted, process steps, and timeline; and confirmation of the quoted rule that Advanced Access requires a verified business. Read the official Business Verification guide.
4. **Standard vs Advanced Access per permission** for `pages_manage_posts`, `pages_read_engagement`, `pages_show_list` (and whether `pages_manage_engagement` / `pages_read_user_content` differ). Read the Permissions Reference.
5. **App Review evidence per permission:** screencast requirements, test credentials, data-handling questionnaire, and the "successful API call within 30 days" rule. Read the App Review submission guide.
6. **App Review timelines and Data Use Checkup:** how long review takes; whether an annual Data Use Checkup is required to retain Advanced Access.
7. **Tech Provider / Access Verification:** does a SaaS serving other businesses' Pages need Tech Provider verification in addition to Business Verification + App Review? This materially affects the onboarding build.
8. **Non-expiring Page tokens:** confirm from the official Access Tokens guide (or Access Token Debugger) that Page tokens derived from a long-lived user token never expire, and confirm the exact invalidation triggers.
9. **Rate limits:** confirm the Platform (200 × users/hour) and BUC (4,800 × engaged users/24h) formulas, headers, and error codes from https://developers.facebook.com/docs/graph-api/overview/rate-limiting/ .
10. **Media limits:** photo max size/formats, video max size/formats and upload method, Reels duration/aspect/resolution/frame-rate/daily cap, post-text character limit, scheduling window. Read the Page Photos, Page Videos, and Reels Publishing references.
11. **Webhooks:** exact payload contents for `feed` and `mentions` fields; whether `pages_manage_metadata` + per-Page `subscribed_apps` is required; whether mention data is queryable. Read the Webhooks for Pages guide.
12. **Platform Terms / Developer Policies (full text):** prohibited practices, data retention/deletion duties, rules on automated posting and inauthentic behaviour, and any rule touching attribution/tracking links. Read https://developers.facebook.com/terms and the Developer Policies in a live browser session.
13. **Page tasks management:** how a Page admin grants the `CREATE_CONTENT` task, and whether `MODERATE` is needed to reply to comments; whether an admin (vs a task holder) must perform the connect flow.
14. **Doc typo:** the getting-started page's `pages_manage_read_engagement` vs `pages_read_engagement` — confirm the correct permission name in the App Dashboard before requesting it.
15. **Current Graph API version and support window:** the getting-started examples use v26.0; the version lifecycle (support duration, sunset dates) was not verified from an official page read.

## Recommended next step (Phase 0 follow-up)

Re-read the 15 open items above from official Meta documentation in a live browser session (the fetch tool was denied by Meta's servers for those pages in this session), then update this document's "Not directly verified" blocks into verified facts or corrections before any build or customer onboarding begins. The highest-priority reads are the Platform Terms, the Permissions Reference, the Business Verification guide, and the Rate Limiting overview.
