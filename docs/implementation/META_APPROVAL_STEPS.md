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
   `https://www.axionory.com/account/auth/x/callback`
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

## 2. Meta: Facebook Pages + Instagram (one app, the longest clock)

*Updated 2026-11-12 for www.axionory.com.* One Meta app covers both
networks. Instagram uses **Instagram API with Facebook Login**: the
customer signs in with Facebook, and Axionory finds the Instagram
Business or Creator account through the Facebook Page it is linked to.
Unified Login, which works without a Page, is a possible later change.

### 2a. Create the app
1. At developers.facebook.com, go to **My Apps**, then **Create App**.
   Choose the use case for managing a Page's content. If the dashboard
   asks for an app type, choose **Business**.
2. Add **Instagram** (API with Facebook Login) to the same app.
3. **Settings, Basic:**
   - App domains: `axionory.com`
   - Privacy Policy URL: `https://www.axionory.com/privacy`
   - Terms of Service URL: `https://www.axionory.com/terms`
   - User data deletion, **Data Deletion Callback URL**:
     `https://www.axionory.com/meta/data-deletion`
   - App icon: `axionory-app-icon-1024.png`
   - Category: Business and Pages
4. **Facebook Login settings:**
   - Valid OAuth Redirect URIs, both, exactly:
     `https://www.axionory.com/account/auth/facebook/callback`
     `https://www.axionory.com/account/auth/instagram/callback`
   - Deauthorize Callback URL: `https://www.axionory.com/meta/deauthorize`
   - Client OAuth login and Web OAuth login: on. Enforce HTTPS: on.

Both callbacks verify Meta's `signed_request` with the app secret, then
delete that person's Facebook and Instagram connections, tokens
included. The data deletion answer points Meta to
`/meta/data-deletion/status?code=...` (see `ventoryn/meta_deletion.py`).

### 2b. Permissions Axionory requests (already in the code)
- **Facebook Page:** `pages_show_list`, `pages_read_engagement`,
  `pages_manage_posts`
- **Instagram:** `instagram_basic`, `instagram_content_publish`,
  `pages_show_list`, `pages_read_engagement`, `business_management`

No messaging or DM permission is ever requested. `business_management`
is there because most Pages sit in a Business portfolio. Without it,
`/me/accounts` does not return the Instagram account linked to such a
Page.

### 2c. Business Verification (start it first)
In **Business Settings, Security Center**, start Business Verification
with the business's legal documents. It runs on Meta's schedule, often
weeks, and App Review for these permissions cannot finish without it.

### 2d. App Review (Advanced Access), one screencast per permission
Until a permission clears review, it only works for people who have a
role on the app (admin, developer, tester). To test with your own
account first, add it under **App Roles**. Then record a screencast for
each permission, using the flow below:

1. On www.axionory.com, go to **Integrations, Facebook**, choose a
   website, and click **Connect with Facebook**.
2. Facebook's login and consent screen: show the permissions it lists,
   then click **Continue**.
3. The Page picker in Axionory: pick the Page. It shows
   **Connected, Facebook Page ... linked** (`pages_show_list`,
   `pages_read_engagement`).
4. Write a draft post on that website's page and click
   **Approve & post**. Show the post on the Facebook Page
   (`pages_manage_posts`).
5. Instagram: **Integrations, Instagram, Connect with Instagram**, then
   Facebook's consent, then **Connected, Instagram Business account
   linked**. Approve a draft and show the post on Instagram
   (`instagram_basic`, `instagram_content_publish`,
   `business_management`).
6. Optional: **Disconnect**, to show that access is removed.

For each permission, explain in plain words: "Axionory is a marketing
assistant. The business owner connects their own Page or Instagram
account, Axionory drafts posts, and nothing is published until the
owner approves each one in Axionory."

### 2e. Instagram limits to keep in mind
- A link in a caption is plain text, not clickable. Put the link in the
  bio or in a Story link sticker.
- The account must be a Business or Creator account linked to a Page
  the customer manages. The person connecting must be able to create
  content on that Page.

## 3. What the server needs after the consoles

Add the keys on the server with typing hidden (never paste them into
chat), and check the lengths before saving:

```bash
ssh ventoryn
cd ~/Projects/AI_Agents_Ecosystem
f=.env.ventoryn
read -rs -p "Meta App ID: " A; echo; echo "id length: ${#A}"
read -rs -p "Meta App Secret: " S; echo; echo "secret length: ${#S}"
# App ID is ~15-16 digits, App Secret is 32 characters. If both look
# right, in the SAME session:
printf 'VENTORYN_FACEBOOK_APP_ID=%s
VENTORYN_FACEBOOK_APP_SECRET=%s
' "$A" "$S" >> $f
unset A S; chmod 600 $f
./deploy/docker/ventoryn.sh deploy
```

Until these are set, the website page says "app registration needed"
and the Connect buttons do not appear. The data deletion and
deauthorize callbacks refuse every request, because without the secret
nothing can be verified.

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
