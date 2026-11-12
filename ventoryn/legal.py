"""Public legal pages: /privacy and /terms.

Written from what Axionory actually does (see ventoryn/analytics.py,
saas.py, saas_routes.py, campaigns.py, core/media_providers.py) — update
them whenever a new provider or a new kind of data is added. Operator:
Spencer Rawlson, Quebec, Canada; Quebec's Law 25 requires naming the
person in charge of the protection of personal information.
"""

from __future__ import annotations

from ventoryn import ui

BRAND = "Axionory"
SITE = "www.axionory.com"
CONTACT = "privacy@axionory.com"
OPERATOR = "Spencer Rawlson"
EFFECTIVE = "November 12, 2026"

_NAV = (f"<p class='muted'><a href='/'>{BRAND}</a> &mdash; <a href='/privacy'>Privacy Policy</a>"
        " &mdash; <a href='/terms'>Terms of Service</a></p>")

PRIVACY = f"""
<h1>Privacy Policy</h1>
<p class='muted'>Effective {EFFECTIVE}</p>
<p>{BRAND} (&ldquo;{BRAND}&rdquo;, &ldquo;we&rdquo;) is an AI customer-acquisition service
operated by {OPERATOR} in Quebec, Canada. This policy explains what personal information we
collect, why, who we share it with, and the choices you have. We follow Quebec&rsquo;s
<em>Act respecting the protection of personal information in the private sector</em> (Law 25)
and Canada&rsquo;s PIPEDA.</p>

<h2>Person in charge of personal information</h2>
<p>{OPERATOR} is responsible for the protection of personal information at {BRAND}.
Contact: <a href='mailto:{CONTACT}'>{CONTACT}</a>.</p>

<h2>What we collect</h2>
<ul>
<li><strong>Your account:</strong> when you sign in with Google we receive your name, email
address and Google account identifier. We store a two-factor authentication secret and backup
codes (protected), and a session cookie that keeps you signed in.</li>
<li><strong>Your websites:</strong> the websites you add, their settings, and the public pages we
read from them (titles, text, sitemap, a screenshot) to analyse them and to draft content.</li>
<li><strong>Connected accounts:</strong> when you connect X, Facebook, Instagram, TikTok, Plausible
or Stripe for a website, we store the access token, API key or webhook secret it needs. Tokens are
encrypted at rest and used only for the actions you approve. From TikTok and other networks we read
basic profile information (such as your display name and avatar) to show which account a post goes to.</li>
<li><strong>Sales:</strong> if you connect Stripe sales for a website, we receive payment events
(amount, currency, payment identifiers, refunds and disputes) to count paying customers. Revenue is
hidden by default and is never written to logs. We never see card numbers.</li>
<li><strong>Billing:</strong> your {BRAND} subscription is processed by Stripe; we keep your Stripe
customer and subscription identifiers and your credit balance.</li>
<li><strong>Agent work:</strong> the reports, article drafts, campaign posts and videos our agents
make for your websites, and what each job used (credits, characters, video seconds).</li>
<li><strong>{BRAND} Analytics (on websites that install it):</strong> page views and events without
cookies or local storage. A visitor is counted with a one-way hash of a daily secret, the site, the IP
address and the browser; the secret changes every day and is deleted, IP addresses and browser strings
are never stored, and raw events are rolled up into daily totals and deleted.</li>
</ul>

<h2>Why we use it</h2>
<p>To provide the service you subscribe to: signing you in securely, analysing your websites, drafting
articles, campaigns and videos for your approval, posting only what you approve, measuring visitors and
verified paying customers, billing, and protecting the service against abuse. We do not sell personal
information and we do not use it for advertising.</p>

<h2>Nothing is published without you</h2>
<p>Our agents only draft. A post, article or video is published only after you approve it, on the
account you connected, and you can disconnect any account at any time.</p>

<h2>Who we share it with</h2>
<p>We use service providers who process data on our behalf, only as needed for the service:</p>
<ul>
<li>Google (sign-in), Stripe (payments and, if you connect it, your sales events), Cloudflare (network
and security);</li>
<li>AI providers (such as OpenAI) to draft text, ElevenLabs for voiceovers and HeyGen for presenter
videos &mdash; they receive the scripts and website text needed for the job;</li>
<li>X, Facebook, Instagram and TikTok &mdash; only the posts you approve, sent to your own account;</li>
<li>search and web-performance services to analyse your public website.</li>
</ul>
<p>Some of these providers are located outside Quebec and Canada, mainly in the United States, so your
information may be processed there. We choose providers that protect it, and we share only what each
needs. We may also disclose information when the law requires it.</p>

<h2>How long we keep it</h2>
<p>We keep your information while your account is open. When you delete your account (from My account),
we revoke the connected tokens where the provider allows it and delete your account, websites,
connections, sales records and agent work. Server backups may keep copies for a limited time before
they are overwritten.</p>

<h2>Your rights</h2>
<p>You can access and correct your information, disconnect accounts, hide your revenue, delete your
account, and ask us to stop using or to port your information. Write to
<a href='mailto:{CONTACT}'>{CONTACT}</a>; we answer within 30 days. You may also contact the
Commission d&rsquo;acc&egrave;s &agrave; l&rsquo;information du Qu&eacute;bec.</p>

<h2>Security</h2>
<p>Two-factor authentication is mandatory for every account, connected-account secrets are encrypted,
sensitive actions ask for a fresh code, and sessions expire. No system is perfectly secure; if an
incident puts your information at risk we will notify you and the authorities as the law requires.</p>

<h2>Cookies</h2>
<p>We use only necessary cookies: your sign-in session and short-lived cookies that complete a
connection to another service. No advertising or tracking cookies.</p>

<h2>Children</h2>
<p>{BRAND} is a business service for people 18 and older.</p>

<h2>Changes</h2>
<p>We will post changes here with a new effective date, and tell account holders about material
changes.</p>
"""

TERMS = f"""
<h1>Terms of Service</h1>
<p class='muted'>Effective {EFFECTIVE}</p>
<p>These terms govern your use of {BRAND}, operated by {OPERATOR} in Quebec, Canada (&ldquo;we&rdquo;).
By creating an account you agree to them.</p>

<h2>The service</h2>
<p>{BRAND}&rsquo;s AI agents analyse your websites, research your audience and draft articles,
social posts, campaigns and videos. You review everything: nothing is published, posted or sent
without your explicit approval. Verified attribution shows which approved actions led to paying
customers when you connect the required accounts.</p>

<h2>Your account</h2>
<p>You must be 18 or older and give accurate information. You are responsible for activity under your
account and must keep two-factor authentication working. You may only add websites and connect
accounts you own or are authorised to manage.</p>

<h2>Subscription, credits and cancellation</h2>
<p>The plan is $20 per month (USD), billed in advance by Stripe, and includes a monthly credit
allowance. Agent jobs use credits at the prices shown in the service; failed jobs are refunded in
credits. Credits have no cash value. You can cancel at any time from your account; the plan stays
active until the end of the paid period. We do not guarantee any number of visitors, customers or
sales.</p>

<h2>Third-party platforms and costs</h2>
<p>Posting to X, Facebook, Instagram or TikTok, and voiceovers and videos from ElevenLabs or HeyGen,
use those services under their own terms. Some charge per use; {BRAND} shows an estimate before you
approve and records what was spent. You are responsible for following each platform&rsquo;s rules,
including labelling AI-generated content where required.</p>

<h2>Your content and approvals</h2>
<p>You keep the rights to your websites and content. You give us the permission needed to process them
to provide the service. Drafts produced by the agents are yours once you approve them. You are
responsible for what you approve and publish: check it for accuracy, rights and compliance.</p>

<h2>Acceptable use</h2>
<p>Do not use {BRAND} for spam, deceptive or illegal content, harassment, impersonation, infringing
material, or to get around a platform&rsquo;s limits, and do not attempt to break or overload the
service. We may suspend accounts that do.</p>

<h2>Availability and changes</h2>
<p>We work to keep {BRAND} running but do not promise uninterrupted service. We may change features or
these terms; material changes will be announced to account holders before they take effect.</p>

<h2>Liability</h2>
<p>To the extent the law allows, {BRAND} is provided &ldquo;as is&rdquo;, and our total liability for
any claim is limited to the amount you paid us in the three months before it. Nothing in these terms
limits rights you have under the consumer protection laws of Quebec or your place of residence.</p>

<h2>Ending the service</h2>
<p>You can delete your account at any time. We may end an account that breaches these terms, with
notice where reasonable.</p>

<h2>Law and contact</h2>
<p>These terms are governed by the laws of Quebec and of Canada applicable there. Questions:
<a href='mailto:{CONTACT}'>{CONTACT}</a>. See also our <a href='/privacy'>Privacy Policy</a>.</p>
"""


def _render(title: str, body: str) -> str:
    return ui.page(title, f"<article class='card v-doc'>{_NAV}{body}</article>",
                   extra_css=ui.DOC_CSS)


def register_legal_pages(app) -> None:
    from fastapi.responses import HTMLResponse

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy_page():
        return _render("Privacy Policy", PRIVACY)

    @app.get("/terms", response_class=HTMLResponse)
    def terms_page():
        return _render("Terms of Service", TERMS)
