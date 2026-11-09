# Instagram API Verification — Ventoryn First-Customer Program, Phase 0

**Date:** 2026-11-08
**Scope:** Inspection only. No code changes, no spending, no credentials, no live actions.
**Intended use verified:** A SaaS customer connects *their own* Instagram Business/Creator account. Ventoryn's agents research opportunities and draft *original* promotional/informational posts; the customer reviews and approves each post inside Ventoryn; Ventoryn publishes only the approved original post to that customer's own account, with a signed attribution link. No automated replies, no unsolicited mentions, no unsolicited DMs, no artificial engagement, no mass posting. Reading comments/mentions on the customer's own posts is a secondary interest.

## Method and honesty note

The task required verification from **official Meta documentation only**. Direct text fetches of `developers.facebook.com` pages were **blocked in this sandbox** (HTTP 403 on `https://developers.facebook.com/docs/instagram-api/` on 2026-11-08). The facts below were therefore verified against **search-indexed summaries of the official Meta pages**, cross-checked across multiple independent 2026 guides that each cite the same official pages and quote their wording. Every claim carries the official Meta doc URL it was checked against. Where secondary sources disagree with each other about what the official page says, the disagreement is flagged rather than resolved by guessing.

**Consequence:** this document is a *draft verification*, not a final proof. Before build, someone with live browser access to `developers.facebook.com` must re-open each cited official page and confirm the flagged items (see "Unverified / open questions"). Nothing in this document should be treated as confirmed by a direct read of the live official page.

**Official pages cited throughout** (all on `developers.facebook.com`):

- Instagram Platform overview: <https://developers.facebook.com/docs/instagram-platform/overview>
- Instagram API with Instagram Login: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login>
- Instagram API with Facebook Login: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login>
- Content Publishing guide: <https://developers.facebook.com/docs/instagram-platform/content-publishing>
- IG User Media reference (`POST /{ig-user-id}/media`): <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media>
- `content_publishing_limit` reference: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit>
- IG Media Comments reference: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-media/comments>
- Permissions Reference: <https://developers.facebook.com/docs/permissions>
- Graph API Rate Limiting: <https://developers.facebook.com/docs/graph-api/overview/rate-limiting>
- Access Tokens guide: <https://developers.facebook.com/docs/facebook-login/guides/access-tokens>
- App Review / Access Levels: <https://developers.facebook.com/docs/app-review> (and the Access Levels page, "Advanced Access now requires Business Verification")
- Instagram webhooks (Instagram Login): referenced via the Instagram API with Instagram Login docs and the Webhooks product docs, <https://developers.facebook.com/docs/graph-api/webhooks>

Current Graph API version at time of writing: **v26.0** (released 2026-07-29), per multiple 2026 guides checked against Meta's changelog.

---

## Verdict

**The intended use case is supported by the official Instagram API, with one hard gate and one design-breaking constraint.**

- **Supported in principle:** publishing original photo/video (Reel) posts to a customer's own Business/Creator account, after the customer's in-app approval, via the official two-step container flow, is exactly what the Instagram API's Content Publishing surface is for.
- **Hard gate (blocks serving real customers until cleared):** Ventoryn must pass **Meta App Review** and obtain **Advanced Access** for each permission, and complete **Business Verification** — because the accounts belong to customers, not to Ventoryn. Standard Access only covers people with a role on Ventoryn's Meta app (admin/developer/tester). This is a process cost (screencast + written use case per permission, legal-entity documents), not a fee, and its timeline is on Meta's clock (see §8 — not officially verifiable as a fixed number of days).
- **Design-breaking constraint:** a URL placed in a feed-post or Reel **caption is plain text, not clickable**. Instagram only makes URLs clickable in bio links (via the Links editor) and Story link stickers. Ventoryn's "signed attribution link" therefore **cannot be a tappable link in the published post itself** unless the customer holds a paid Meta One subscription (see §6). The pilot must redesign attribution as plain-text URL in the caption plus "link in bio" wording, or as a Story link sticker — or drop the in-post link requirement.
- **Cost:** Meta charges **no fee** for the Instagram API — no per-call charge, no paid tier, no licence fee. The real costs are engineering time, the App Review / Business Verification cycle, and rate-limit headroom (see §9).
- **Nothing in the intended use case is prohibited** by the API surface itself: no automated replies, no unsolicited DMs/mentions, no artificial engagement are planned, and none are needed.

---

## 1. Account requirement

- **Only Instagram professional accounts can use the API — Business or Creator.** Personal accounts have no API access at all. Meta's Instagram Platform documentation describes the APIs as serving "Instagram professional accounts including both businesses and creators."
  - Source: <https://developers.facebook.com/docs/instagram-platform/overview>
- **Personal accounts are excluded, full stop.** A personal account must be converted to Business or Creator inside the Instagram app (Settings → Account type) before it can connect; conversion is free and keeps followers and posts.
  - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login>
- **The Instagram Basic Display API (the old personal-account, read-only API) was shut down on 2024-12-04** and is not coming back. It must not be built on.
  - Source: <https://developers.facebook.com/docs/instagram-platform/overview> (shutdown announced at <https://developers.facebook.com/blog/post/2024/09/04/update-on-instagram-basic-display-api/>)
- **Two login paths exist; only one needs a Facebook Page:**
  - **Instagram API with Instagram Login** ("Business Login for Instagram"): the user signs in with their Instagram account; **no Facebook Page is required** and no Facebook account link is required. API host is `graph.instagram.com`. This is the path Meta recommends for new integrations, and the right path for Ventoryn.
    - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login>
  - **Instagram API with Facebook Login**: the Instagram professional account **must be linked to a Facebook Page**; auth is Facebook Login; API host is `graph.facebook.com`. This path additionally unlocks hashtag search and `business_discovery` (reading basic public metadata of *other* Business/Creator accounts), which the Instagram Login path does not offer.
    - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login>
- **Linking to a Facebook Page (Facebook Login path only):** in the Instagram app, Settings → Account → Linked accounts / Account Centre, link the Facebook Page; or create the Page at `facebook.com/pages/create` first. On the Instagram Login path this step does not exist.
  - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login>
- **Ventoryn implication:** the pilot should use **Instagram Login** and require each customer to hold a Business or Creator account. Customers with personal accounts must convert first (a customer step, §10). Do **not** choose the Facebook Login path unless the pilot later needs hashtag search or competitor discovery — it adds a Page-linking step for every customer for no pilot benefit.

## 2. Required scopes/permissions (Instagram Login path)

Permission names were renamed on **2025-01-27**: the old `business_basic` / `business_content_publish` names are deprecated; the current names carry the `instagram_business_` prefix. Old names in 2023-era code will fail.

| Permission | What it allows | Needed for pilot? |
|---|---|---|
| `instagram_business_basic` | Read basic profile metadata (username, account ID, account type), the account's media, follower count, comments and mentions. It is the base permission and a dependency of the others below. | **Yes — required.** Reading the account and its posts, and it is also a precondition for refreshing long-lived tokens (§3). |
| `instagram_business_content_publish` | Create organic feed photo and video posts (including Reels, carousels, Stories) on behalf of the account — i.e. create media containers and publish them. | **Yes — required.** This is the publishing permission. |
| `instagram_business_manage_comments` | Read, reply to, hide and delete comments on the account's media; enables the `comments` webhook. | **Only if** the pilot reads/replies to comments. Separate App Review submission from the publishing permission. |
| `instagram_business_manage_insights` | Read account and media insights (reach, views, engagement, follower demographics). | Optional — only if the pilot reports post performance. |
| `instagram_business_manage_messages` | Read/send Instagram DMs. | **No — not requested.** Ventoryn plans no DMs. Do not request it; every extra permission is a separate review. |

Sources: <https://developers.facebook.com/docs/permissions> and <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login>

**Facebook Login path equivalents (for reference only, not the pilot path):** `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_insights`, plus Page permissions (`pages_read_engagement`, `pages_show_list`). The two families are **not interchangeable**: `instagram_business_*` only works with Instagram Login at `graph.instagram.com`; `instagram_*` only works with Facebook Login at `graph.facebook.com`.
- Source: <https://developers.facebook.com/docs/permissions>

**Minimum pilot scope set:** `instagram_business_basic` + `instagram_business_content_publish`. Add `instagram_business_manage_comments` only if comment reading/replying is in the pilot scope, and budget a separate review for it.

## 3. Token lifetimes and how tokens are obtained

Flow (Instagram Login / Business Login for Instagram):

1. **Authorization:** Ventoryn sends the customer to the Instagram authorization window (`https://www.instagram.com/oauth/authorize`); the customer logs in with *their Instagram account* and grants the requested scopes. Ventoryn never sees the customer's Instagram password.
   - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login>
2. **Code exchange:** Ventoryn exchanges the authorization code for a **short-lived token, valid ~1 hour**.
   - Endpoint (Instagram Login path): `POST https://api.instagram.com/oauth/access_token`
   - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login>
3. **Exchange for long-lived token:** the short-lived token is exchanged server-side for a **long-lived token valid 60 days** (`expires_in` ≈ 5,184,000 seconds). This exchange uses the app secret, so it must happen server-side.
   - Endpoint: `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret={app-secret}&access_token={short-lived-token}`
   - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login>
4. **Refresh:** a long-lived token is refreshed **in place** — there is no separate refresh token. Conditions, all three required:
   - the token is **at least 24 hours old**;
   - the token is **still unexpired**;
   - the user granted **`instagram_business_basic`**.
   - Endpoint: `GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token={long-lived-token}`
   - The refreshed token is a **new token valid for another 60 days** from the refresh. The old token string should be replaced (it may stop working).
   - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login> (refresh reference)
5. **Expiry is terminal:** a token not refreshed within 60 days "will expire and can no longer be refreshed" — the only fix is sending the customer through login again.
   - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login>

**Practical consequence:** Ventoryn needs a scheduled job that refreshes every stored token well before expiry (common practice: every ~50 days, or weekly once a token is more than a few days old). A token that lapses silently stops publishing for that customer with no warning.

**Facebook Login path, for contrast:** short-lived user token (~1 hour) → long-lived user token (60 days) via `grant_type=fb_exchange_token` at `graph.facebook.com`; a Page token derived from a long-lived user token does not expire on its own. Not the pilot path.
- Source: <https://developers.facebook.com/docs/facebook-login/guides/access-tokens>

**Unverified detail:** the exact authorization-URL parameter set for Business Login (e.g. whether `force_reauth` / `enable_fb_login` parameters are current) changed during 2025–2026 and was not pinned down from an official page in this pass — see "Unverified / open questions".

## 4. The container publishing flow

Publishing is a **two-step, asynchronous** process. There is no single "create post" call and no direct binary upload when creating a container — media is referenced by public URL (with a separate resumable-upload flow for large video; see below).

**Step 1 — Create a media container:**
- `POST /{ig-user-id}/media` on `https://graph.instagram.com/{version}`
- Parameters (feed image): `image_url` (publicly accessible JPEG URL), `caption` (optional), plus optional `user_tags`, `alt_text`, `location_id`.
- For video/Reels: `media_type=REELS` (or legacy `VIDEO`) with `video_url`; optional `cover_url` (thumbnail), `thumb_offset`.
- For a carousel: create one child container per item with `is_carousel_item=true`, then a parent container with `media_type=CAROUSEL`, `children={id1,id2,…}`, `caption`. Max 10 items; images and videos may be mixed; the carousel counts as **1 post** against the publishing limit.
- For a Story: `media_type=STORIES` with `image_url` or `video_url`. No interactive stickers (poll, link, question, countdown, location, music) via API.
- Returns a container ID. The container is **not** a published post yet.
- Source: <https://developers.facebook.com/docs/instagram-platform/content-publishing> and <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media>

**Step 2 — Poll container status until ready:**
- `GET /{ig-container-id}?fields=status_code`
- Status values: `IN_PROGRESS` (still processing), `FINISHED` (ready to publish), `ERROR` (processing failed), `EXPIRED` (not published within 24 hours), `PUBLISHED` (already published).
- **Publish only when the status is `FINISHED`.** Meta's guidance reported from the docs: poll about once per minute, for no more than ~5 minutes, for images; video/Reels processing takes longer (30 seconds to several minutes).
- **Containers expire after 24 hours** if not published. Do not create containers days ahead — store the post data (image URL, caption, time) and create the container at publish time.
- Source: <https://developers.facebook.com/docs/instagram-platform/content-publishing> and <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media>

**Step 3 — Publish the container:**
- `POST /{ig-user-id}/media_publish?creation_id={container-id}&access_token={token}`
- Returns the published media ID (`{id: ig-media-id}`). The post goes live immediately.
- Source: <https://developers.facebook.com/docs/instagram-platform/content-publishing>

**Media hosting requirement:** Meta fetches the media by cURL from the URL supplied, so the image/video **must be on a publicly accessible HTTPS server at publish time**, with correct `Content-Type` headers (`image/jpeg`, `video/mp4`). Localhost/private URLs fail. Ventoryn must host drafted media at a public URL (e.g. its own CDN/object storage) before calling the API.
- Source: <https://developers.facebook.com/docs/instagram-platform/content-publishing>

**Large video alternative (unverified in this pass):** a resumable upload flow exists — `POST /media` with `upload_type=resumable`, then upload to `https://rupload.facebook.com/ig-api-upload/{version}/{container-id}`. Its exact current parameters were not confirmed against an official page; see open questions. The URL-reference flow is sufficient for the pilot if videos stay within the URL flow's limits.

## 5. Posting limits

- **Content publishing cap — the headline number is disputed between Meta's own pages.** The Content Publishing guide states: "Instagram accounts are limited to **100 API-published posts within a 24-hour moving period**." The `content_publishing_limit` endpoint reference states the quota total is **currently 50**. A carousel subsection on the same guide also says 50. Both numbers are live on official pages; either can change.
  - Sources: <https://developers.facebook.com/docs/instagram-platform/content-publishing> and <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit>
- **The safe rule, stated by Meta's own design:** do **not** hardcode 50 or 100. Query the live quota before publishing:
  - `GET /{ig-user-id}/content_publishing_limit` returns `quota_total`, `quota_usage`, and `quota_duration` (86,400 seconds = 24 h) for that specific account.
  - The window is a **rolling 24-hour window**, not a calendar day — a post published at 3:00 PM yesterday still counts until 3:00 PM today.
  - Stories, feed posts, Reels and carousels (one carousel = one post) all count toward the same cap.
  - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit>
- **General API rate limit:** **200 calls per hour per user** (per app+user pair), rolling window. Polling container status, reading comments and publishing all draw from this budget.
  - Source: <https://developers.facebook.com/docs/graph-api/overview/rate-limiting>
- **Business Use Case (BUC) rate limit:** the Instagram Platform is also governed by a BUC budget sized as **4,800 calls per 24 hours × the account's impressions** in that window (with a minimum baseline). Small/new accounts with few impressions therefore have a small call budget — relevant if Ventoryn polls aggressively across many low-reach customer accounts.
  - Source: <https://developers.facebook.com/docs/graph-api/overview/rate-limiting>
- **Pilot implication:** the pilot's volume (a handful of approved posts per customer per day) is far below either publishing cap. The binding constraints are the 200 calls/hour/user budget (design polling accordingly) and the BUC formula for low-impression accounts — not the 50/100 post cap. Still, implement the `content_publishing_limit` pre-check rather than hardcoding a number.

## 6. Media rules

All figures below are from the IG User Media reference as checked against Meta's docs in mid-2026 (v25.0/v26.0). Where secondary sources conflict, the conflict is flagged.

**Feed images:**
- Format: **JPEG only**. PNG, WebP, GIF are rejected for feed posts; extended JPEG formats (MPO, JPS) are also unsupported.
- Max file size: **8 MB**.
- Aspect ratio: **must be within 4:5 (portrait) to 1.91:1 (landscape)**; square 1:1 is fine. Outside this range, container creation fails (error 36003 / subcode 2207009).
- Width: 320–1440 px (Meta auto-scales outside this); colour sRGB (Meta auto-converts).
- Source: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media>

**Feed video / Reels** (all single-video uploads publish as Reels; there is no separate standalone feed-video format via API):
- Container/codec: **MP4 or MOV, H.264 (or HEVC), AAC audio**; `moov` atom at the front of the file.
- Duration: **3 seconds to 15 minutes**.
- Max file size: **300 MB** per the IG User Media reference. ⚠️ One secondary source claims 1 GB; the 300 MB figure is the one tied to the reference page — treat 300 MB as the working limit and re-confirm live.
- Aspect ratio: 0.01:1 to 10:1 accepted; **9:16 recommended** (1080×1920); frame rate 23–60 fps.
- ⚠️ **Conflict flagged:** several 2026 guides state API-published Reels are capped at **90 seconds** despite the reference saying 15 minutes. The 90-second figure may reflect an older limit or a different media type. Do not promise customers Reels over 90 seconds until the live reference is re-checked.
- Source: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media>

**Carousels:** max **10 items** (images, videos, or mixed); the in-app limit is higher (20), the API stays at 10. All carousel images are cropped to the first item's aspect ratio.
- Source: <https://developers.facebook.com/docs/instagram-platform/content-publishing>

**Stories (if ever used):** image JPEG or video MP4/MOV; video max **60 seconds**; Story video file size reported as 100 MB (one source) vs 8 MB (another, older) — **unverified, re-check live**. No interactive stickers via API. Stories expire after 24 hours live; containers expire after 24 hours unpublished.
- Source: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media>

**Captions:**
- Maximum **2,200 characters**, **30 hashtags**, **20 @mentions** per caption.
- Source: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media>

**Text-only posts are not possible.** Every publish call requires media (`image_url` or `video_url`). A draft with text and no image/video cannot be published to Instagram via the API. Ventoryn must always generate or attach an image (or video) for every Instagram post — a text-only draft must be blocked in the composer or paired with media before approval.
- Source: <https://developers.facebook.com/docs/instagram-platform/content-publishing> (the endpoint requires a media URL; no text-only media type exists)

**URLs / links in captions — the attribution problem:**
- A URL typed into a feed-post or Reel caption is rendered as **plain text and is not clickable/tappable**. Instagram does not make caption URLs clickable, by design (spam prevention / keeping users in-app). No third-party tool can override this — it is Instagram-side behaviour.
- Clickable links exist only in **(a)** bio links added via the profile Links editor, and **(b)** Story link stickers. @mentions and hashtags in captions are tappable but lead only to other places inside Instagram.
- ⚠️ **New paid exception (unverified against an official page in this pass):** Meta One (launched September 2026) reportedly lets Advanced-tier subscribers add a clickable link to a limited number of posts/Reels per month; Meta Verified subscribers were also reported to be in a caption-links test. This is a **customer-paid subscription**, not something Ventoryn can grant via the API, and availability varies by country/account. Do not build the pilot around it.
- **Ventoryn implication:** the "signed attribution link" **cannot be delivered as a tappable link inside the published Instagram post** through the API. Options: (i) include the short plain-text URL in the caption plus "link in bio" wording (customer puts the link in their bio); (ii) publish a companion Story with a link sticker; (iii) put attribution in the image itself; (iv) drop the in-post link for Instagram and keep attribution in Ventoryn's own records. This needs a product decision before the pilot — it is not an engineering workaround.
- Sources: Instagram Help Center behaviour as reported against Meta's docs (caption links not clickable); API-side confirmation that no link parameter exists on the media endpoints — <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media>. The Meta One detail could **not** be verified against an official Meta page in this pass.

## 7. Comment and mention handling

**What is available (Instagram Login path):**

- **Read comments on the customer's own posts:** `GET /{ig-media-id}/comments` returns comments for a media object. Requires `instagram_business_manage_comments` (or `instagram_business_basic` for basic read, per the permission reference — the comments permission is the one that grants full read/reply/hide/delete).
  - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-media/comments>
- **Reply to comments:** `POST /{ig-media-id}/comments` (reply) and `POST /{ig-comment-id}/replies`; hide and delete are also supported. Requires `instagram_business_manage_comments`, separately App-Review-approved.
  - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-media/comments>
- **Mentions:** the Webhooks product supports `mentions` (user mentions the account) and `comments` fields. On the Instagram Login path, webhook subscription is per-account via `POST /me/subscribed_apps?subscribed_fields=comments,messages` on `graph.instagram.com`, with the callback URL and verify token configured in the app dashboard. Requirements reported: app in **Live** mode, **Advanced Access** for the `comments` permission, and a **public** account (private accounts do not receive comment/mention webhooks).
  - Source: <https://developers.facebook.com/docs/graph-api/webhooks> and <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login>

**What is contested / not reliably available:**

- ⚠️ **Comment webhooks are disputed.** Meta's webhook docs list `comments` and `mentions` fields, and some 2026 guides describe them working (including a `comments` webhook carrying mention events). Other 2026 guides state flatly that **there is no webhook for new comments** and that apps must **poll** `GET /{media-id}/comments` — and that the Meta Webhooks product covers "some Instagram events (mentions, story interactions) but comment webhooks are not available for most Business accounts." This contradiction was **not resolvable** from indexed summaries alone. **Design assumption for the pilot: poll for comments; treat webhooks as a bonus to verify live.** Polling must respect the 200 calls/hour/user budget (§5).
- **Story insights webhooks, hashtag search, `business_discovery`, product tagging, partnership ads:** not available on the Instagram Login path (hashtag search and `business_discovery` need the Facebook Login path). Not needed for the pilot.
  - Source: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login>
- **Ventoryn implication:** reading comments on the customer's own posts is supported (poll-based at minimum); replying is supported but requires the extra permission and review. Ventoryn plans **no automated replies** — any reply feature should be human-approved in-product, and is out of pilot scope unless explicitly added. Mentions can be received via webhook *if* the live docs confirm delivery; do not promise real-time mention alerts in the pilot.

## 8. App Review and Advanced Access (SaaS posting to customers' accounts)

This is the **gating section** for Ventoryn: everything else is API mechanics; this is what decides whether real customers can connect.

- **Two access levels per permission:**
  - **Standard Access** — the permission works only for people who hold a role on the app (admin, developer, tester). Enough to build and test against Ventoryn's own / test accounts.
  - **Advanced Access** — the permission works for any real user who grants it. **Required the moment the app serves Instagram accounts Ventoryn does not own or manage** — i.e. every first customer.
  - Source: <https://developers.facebook.com/docs/app-review> (Access Levels)
- **Advanced Access requires two things, in order:**
  1. **Business Verification** — Meta verifies the legal business exists: official legal name, address, and an authorized representative, evidenced by official documents (e.g. incorporation / registration papers; name must match the Meta Business Portfolio exactly). Completed in Meta Business Suite → Security Center → Business Verification. Business Verification alone does **not** unlock real users — it only lets the app be submitted for App Review.
  2. **App Review, per permission** — each permission (`instagram_business_basic`, `instagram_business_content_publish`, and `instagram_business_manage_comments` if requested) is reviewed **independently**. Meta asks: how does the app use this permission, and a **step-by-step screencast showing the permission in action** in the real product. Reviewers log into the app and test every permission requested; if they cannot access it, the whole submission is rejected. The app must be in **Live mode** before Advanced Access can be requested, and going Live requires Business Verification first. A privacy policy covering each permission's data use and a working data-deletion callback are also required.
  - Sources: <https://developers.facebook.com/docs/app-review> and <https://developers.facebook.com/docs/permissions>
- **Evidence Meta asks for (per permission):** written use-case explanation; screencast demonstrating the exact flow (for Ventoryn: connect account → agent drafts → customer approves in-product → publish → post visible on the account); test credentials / a test user account reviewers can log in with; privacy policy URL; data-deletion callback URL.
  - Source: <https://developers.facebook.com/docs/app-review>
- **Timeline: NOT officially verifiable as a fixed number.** Secondary sources variously report "3–7 business days for most permissions", "2–4 weeks for sensitive permissions", and "two to four weeks for the pair" (App Review + Business Verification), plus at least one round of revisions as common. **No Meta page found in this pass publishes a guaranteed review time.** Budget weeks, not days, and at least one resubmission round. Do not promise a customer a go-live date that depends on App Review.
  - Source: timeline figures are **not** from an official page — see "Unverified / open questions".
- **October 2026 change (unverified against an official page):** Meta reportedly removed the separate **access verification (Tech Provider review)** for apps accessing clients' business data (~Oct 9, 2026). If true, that removes one gate; App Review and Business Verification still apply. Flagged as unverified — confirm on the live App Review docs before planning around it.
- **Development-mode testing:** while in Development mode, the app works immediately for app-role users and test users, with no review. The pilot can therefore be built and demoed end-to-end on Ventoryn-owned/test accounts before any review is submitted.
  - Source: <https://developers.facebook.com/docs/app-review>
- **Ventoryn implication:** sequence the pilot as (1) build against Standard Access with test accounts, (2) start Business Verification early (it is on the critical path and needs legal documents), (3) submit App Review per permission with a real screencast of the approval flow, (4) only then onboard paying customers. The approval-first design (customer approves every post in Ventoryn) is actually helpful evidence for review — it demonstrates human control and no spam/automation.

## 9. Pricing

- **The Instagram API is free.** Meta publishes no rate card, bills nothing per call, and has no paid tier for the Instagram Platform / Graph API. There is no per-call charge, no per-account charge, no licence fee, and no subscription to buy more volume. This is stated consistently across every 2026 source checked against Meta's docs; Meta's own documentation lists no fee for these endpoints.
  - Source: absence of any fee schedule on <https://developers.facebook.com/docs/instagram-platform/overview> and <https://developers.facebook.com/docs/permissions>; corroborated by Meta's App Review docs, which describe review as free.
- **What is metered instead of charged:** rate limits (§5) — 200 calls/hour/user, the BUC formula (4,800 × impressions / 24 h), and the per-account publishing cap (50 or 100 posts / rolling 24 h, §5). There is no way to pay Meta to raise these.
  - Source: <https://developers.facebook.com/docs/graph-api/overview/rate-limiting>
- **What it actually costs:** App Review and Business Verification are free in money but cost engineering and calendar time (screencast, use-case write-ups, legal documents, review cycles); a Meta developer account and a Business-type Meta app are free; the Instagram professional account is free; token-refresh automation, public media hosting, and polling infrastructure are Ventoryn's own engineering/running costs.
  - Source: <https://developers.facebook.com/docs/app-review>
- **Third-party aggregators are not Meta pricing:** unified posting APIs (e.g. Ayrshare, Buffer, Outstand, Blotato) charge their own subscriptions to skip the review cycle using their own Meta app. That is a build-vs-buy decision, not a Meta fee — and using one would mean customer tokens live under someone else's app. Not recommended as the default without a separate evaluation.
- **Meta One caveat:** if the attribution-link problem (§6) is "solved" via Meta One, that is a **customer-paid subscription** (reported from September 2026, Advanced tier from $49.99/month — price not verified against an official page). It is not a Ventoryn API cost and not something Ventoryn can purchase on the customer's behalf through the API.

## 10. What Ventoryn must build vs what the customer does in Meta's consoles

### Ventoryn builds / operates

- A **Business-type Meta app** at `developers.facebook.com`, with the Instagram product added and the "API setup with Instagram login" configuration completed (OAuth redirect URIs, Instagram app ID/secret — note: these are **separate** from the Facebook App ID/secret).
- The **OAuth (Business Login) flow**: authorization redirect, code exchange, short→long-lived token exchange (server-side, app secret never shipped to clients), encrypted token storage.
- A **token-refresh job**: refresh every stored long-lived token before its 60-day expiry (only refreshable when ≥24 h old and unexpired); on expiry, force re-login.
- **Public media hosting** for every image/video to be published (HTTPS, correct Content-Type), since Meta fetches media by URL.
- The **container flow**: create container → poll `status_code` to `FINISHED` → `media_publish`; honour `content_publishing_limit` before publishing; enforce the per-account cap and the 200 calls/hour budget.
- **Media validation** before container creation: JPEG-only feed images, 4:5–1.91:1 aspect, ≤8 MB; Reels MP4/MOV H.264, 3 s–15 min, ≤300 MB (working limit); caption ≤2,200 chars / 30 hashtags / 20 mentions; **block text-only drafts** for Instagram.
- The **human approval gate**: draft → customer review/approve inside Ventoryn → publish only approved posts. (Also the strongest evidence for App Review.)
- **Comment polling** (if in scope) and the `instagram_business_manage_comments` permission/review; webhook endpoint + `POST /me/subscribed_apps` per account if comment/mention webhooks are later confirmed live.
- **Business Verification** (legal documents, Meta Business Suite) and **App Review submissions** (per-permission screencast + use case + test credentials + privacy policy + data-deletion callback).
- App **Live mode** switch once review passes.

### The customer does (in Instagram / Meta consoles, not in Ventoryn)

- Hold or convert to an **Instagram Business or Creator account** (free, in the Instagram app: Settings → Account type). Personal accounts cannot connect.
- **Connect via Instagram Login**: sign in on Instagram's own authorization screen and grant `instagram_business_basic` + `instagram_business_content_publish` (and comments permission if applicable). Ventoryn never asks for or sees their Instagram password.
- **Re-connect if their token lapses** (if Ventoryn misses the 60-day refresh window) or if they revoke access.
- (Only if the Facebook Login path were chosen — not recommended: create/link a Facebook Page. Not needed on the Instagram Login path.)
- (Only if they want a clickable attribution link: manage their own bio link, or hold their own paid Meta One subscription — see §6.)
- Keep their account **public** if comment/mention webhooks are ever relied on (private accounts do not receive them, per secondary reports).

### Shared / either side

- The privacy policy and data-deletion handling are Ventoryn's, but the customer must be told what data Ventoryn reads (profile, media, comments if granted) at consent time — the consent screen shows the permission list.

---

## Supported / Restricted / Unavailable — pilot activity table

| Activity (pilot scope) | Status | Basis |
|---|---|---|
| Customer connects own Business/Creator account via Instagram Login | **Supported** | §1, §3 |
| Customer connects a personal account | **Unavailable** | Personal accounts have no API access; must convert first (§1) |
| Agent drafts original post; customer approves in Ventoryn; Ventoryn publishes approved post | **Supported** | §2, §4 — this is the Content Publishing use case |
| Publish single photo post (JPEG) | **Supported** | §4, §6 |
| Publish Reel (video) | **Supported** (with caveats) | §4, §6 — duration cap disputed (90 s vs 15 min); file-size working limit 300 MB |
| Publish carousel (up to 10 items) | **Supported** | §4, §6 |
| Publish Story | **Supported** (limited) | §4, §6 — no interactive stickers; video size limit unverified |
| Publish text-only post | **Unavailable** | Every publish requires media (§6) |
| Include a **clickable** signed attribution link in the caption | **Unavailable** via API | Caption URLs are plain text (§6); paid Meta One exception is customer-side and unverified |
| Include attribution as plain-text URL in caption + "link in bio" | **Supported** | §6 |
| Read customer's own posts/media/profile | **Supported** | `instagram_business_basic` (§2) |
| Read post insights (reach, views, engagement) | **Supported** (extra permission) | `instagram_business_manage_insights` (§2) |
| Read comments on customer's own posts | **Supported** | `instagram_business_manage_comments`; poll-based at minimum (§7) |
| Reply to comments | **Supported** (extra permission + review; human-approved only in pilot) | §7 — no automated replies are planned |
| Receive comment/mention webhooks in real time | **Restricted / unverified** | Webhook fields exist in Meta docs but delivery for comments is disputed (§7); design for polling |
| Automated replies / unsolicited DMs / unsolicited mentions / artificial engagement / mass posting | **Not planned; do not build** | Outside the verified use case; DM automation would need `instagram_business_manage_messages` + review and is out of pilot scope |
| Hashtag search / reading other accounts' data (`business_discovery`) | **Unavailable on Instagram Login path** | Needs Facebook Login path + Page (§1, §7); not needed for pilot |
| Schedule a post for later via the API | **Restricted** | No native scheduling; containers expire after 24 h — store post data and create the container at publish time (§4) |
| Serve real (non-test) customer accounts | **Blocked until App Review + Business Verification pass** | Advanced Access required (§8) |
| API usage fees from Meta | **None** | §9 |

---

## Unverified / open questions

Items below could **not** be verified against an official Meta page in this pass (direct fetch of `developers.facebook.com` returned HTTP 403 in this sandbox; facts rest on indexed summaries). Each needs a live re-check before build commitments are made.

1. **Direct read of the live official pages.** The entire document rests on search-indexed summaries of Meta's docs, not on a direct read. Re-open every URL in the method note with live browser access and confirm the quoted rules, especially §§4–6.
2. **Publishing cap: 50 or 100?** Meta's own pages disagree (Content Publishing guide: 100 per 24 h moving period; `content_publishing_limit` reference and carousel subsection: 50). Resolution: implement the runtime `content_publishing_limit` check and never hardcode either number (§5).
3. **Reels duration cap: 90 seconds or 15 minutes?** The IG User Media reference is reported as 3 s–15 min; several 2026 guides say API Reels cap at 90 s. Re-check the live `#reels-specs` section before promising >90 s Reels.
4. **Reels max file size: 300 MB or 1 GB?** Reference-tied figure is 300 MB; one guide claims 1 GB. Re-check live.
5. **Story video file size: 8 MB or 100 MB?** Sources conflict; Stories are out of pilot scope, but confirm before offering them.
6. **Comment webhooks: real-time delivery or poll-only?** Meta's webhook docs list `comments`/`mentions` fields, but multiple 2026 guides report no comment webhook delivery for most Business accounts on the Instagram Login path. Confirm against the live Webhooks docs and, if needed, by a live test; design for polling meanwhile.
7. **App Review timeline.** No official page found publishing a guaranteed review duration. Figures in circulation (3–7 business days; 2–4 weeks) are vendor/guide estimates. Do not commit a customer go-live date to any of them.
8. **Meta One caption links.** The September 2026 paid caption-link feature (and its pricing/tiers) could not be verified against an official Meta page. Do not plan attribution around it.
9. **Access verification removal (Oct 2026).** Reports that Meta retired the separate Tech Provider / access-verification review need confirmation on the live App Review docs.
10. **"Instagram API with Unified Login" third setup.** Mentioned in one November 2026 source as Meta's newly recommended setup using the `instagram_*` permission family; not corroborated elsewhere in this pass. Check the live Platform overview — if it supersedes the two-path model, §§1–3 need revision.
11. **Resumable video upload (`rupload.facebook.com`) exact parameters.** Existence is well attested; exact current parameters were not pinned to an official page. Only needed if the pilot outgrows the URL-reference flow.
12. **Business Login authorization-URL parameters.** The exact current parameter set (and any 2025–2026 changes such as `force_reauth`) was not pinned to an official page in this pass.
13. **Exact screencast / evidence checklist for `instagram_business_content_publish` App Review.** The general requirement (screencast + use case + test credentials) is documented; the permission-specific evidence expectations should be read from the live App Review submission form when Ventoryn applies.
14. **Whether `instagram_business_basic` alone suffices to *read* comments** (vs requiring `instagram_business_manage_comments` for any comment read). Sources differ; the comments permission is the safe assumption for the pilot.

---

## Sources consulted (method)

- Official Meta documentation URLs listed in the method note above (via search-indexed summaries; direct fetch blocked with HTTP 403 on 2026-11-08).
- Independent 2026 guides checked against those official pages, used only as corroboration, not as authority: PostZen Instagram Graph API Guide (checked against Meta docs 2026-09-03), Social-Scheduler reference (verified against live Meta docs 2026-07-22), SpeedRunSocial size limits (checked 2026-08-06), ContentIQ Instagram rules (verified 2026-10-21), Vimitron token guide (2026-10-19), Socialift Instagram Login setup (2026-09-19), Outstand/Blotato pricing guides (2026-09), and the Meta developer-docs mirror excerpts on GitHub. Where these guides disagreed with each other about an official page, the disagreement is recorded above rather than settled.

*End of draft verification — Phase 0, inspection only.*
