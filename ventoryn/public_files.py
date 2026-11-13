"""robots.txt, sitemap.xml, llms.txt and the brand image files for
Axionory's own public site.

Only the landing page is public; everything else needs a sign-in, so
the sitemap lists just "/" and robots.txt keeps crawlers out of the
signed-in areas. llms.txt is a short plain-text summary for AI
assistants, written from the landing page's own copy — no claims that
page does not make.
"""

from pathlib import Path

from fastapi.responses import FileResponse, PlainTextResponse, Response

#: The Axionory logo (nav brand) and favicon — the one deliberate
#: exception to the vector-only rule (Spencer, 2026-11-12): a
#: supplied brand asset, not a generated graphic.
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Signed-in or machine-only areas: nothing there for a crawler.
PRIVATE_PREFIXES = (
    "/account/", "/api/", "/admin", "/portfolio", "/businesses",
    "/experiments", "/approvals", "/reports", "/report", "/ops", "/game",
    "/campaigns", "/agents", "/analytics", "/integrations", "/customers",
    "/websites/",
)

LLMS_TXT = """\
# Axionory

> Axionory is an AI customer-acquisition service for websites. You enter your website and connect your accounts; AI agents discover your audience, prepare campaigns, and identify opportunities. You approve every action before anything happens, and progress is measured in verified paying customers, not page views.

## What it does

- AI audience discovery: relevant conversations, communities, and audience segments found from your website and offering, not from a bought list.
- Prepared outreach: posts, replies, articles, and videos drafted for your review. Nothing is published, replied to, or sent without your explicit approval.
- Verified attribution: every approved action carries a signed attribution link. A customer is counted as verified only after a signature-verified Stripe payment is matched to that link; refunds and lost disputes are removed from the count. Sales with no evidence are shown as unattributed, never guessed.
- Revenue privacy: revenue is private by default; showing revenue amounts and showing the customer count are separate settings.
- Free site analysis: a site score, an answer-engine (GEO) score, and the top things to fix.

## Who it is for

SaaS founders, independent software developers, startup teams, online service businesses, digital product creators, and founders launching their first website.

## Connections

Per website: X and TikTok (posting), Stripe (sales, read-only: Axionory can never charge or refund), and Plausible (analytics, optional). Facebook Pages and Instagram are coming once Meta approves Axionory.

## Pricing

One plan: $20/month with 1,000 credits every month and a growth report per site. Credits: site + GEO audit 5, GEO audit 5, community listening 5, social report 5, SEO article 10, video render 15. No free trial. No guarantee on customer numbers. Cancel anytime from your account.

## Security

Two-factor authentication (TOTP) is mandatory for every account. Stored tokens are encrypted at rest with minimum scopes, and sensitive actions ask for a fresh two-factor code.

## Links

- [Home, pricing and FAQ]({base}/)
- [Sign in]({base}/account/login)
"""


def _base(config) -> str:
    return (getattr(config, "base_url", "") or "").rstrip("/")


def register_public_files(app, config) -> None:
    @app.api_route("/robots.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
    def robots_txt():
        lines = ["User-agent: *", "Allow: /$"]
        lines += [f"Disallow: {p}" for p in PRIVATE_PREFIXES]
        lines += ["", f"Sitemap: {_base(config)}/sitemap.xml", ""]
        return PlainTextResponse("\n".join(lines),
                                 headers={"Cache-Control": "public, max-age=3600"})

    @app.api_route("/sitemap.xml", methods=["GET", "HEAD"])
    def sitemap_xml():
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f"  <url><loc>{_base(config)}/</loc>"
                "<changefreq>weekly</changefreq><priority>1.0</priority></url>\n"
                f"  <url><loc>{_base(config)}/privacy</loc><changefreq>yearly</changefreq></url>\n"
                f"  <url><loc>{_base(config)}/terms</loc><changefreq>yearly</changefreq></url>\n"
                "</urlset>\n")
        return Response(body, media_type="application/xml",
                        headers={"Cache-Control": "public, max-age=3600"})

    @app.api_route("/llms.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
    def llms_txt():
        return PlainTextResponse(LLMS_TXT.replace("{base}", _base(config)),
                                 headers={"Cache-Control": "public, max-age=3600"})

    @app.api_route("/favicon.png", methods=["GET", "HEAD"])
    def favicon_png():
        return FileResponse(STATIC_DIR / "favicon.png", media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.api_route("/static/logo.png", methods=["GET", "HEAD"])
    def logo_png():
        return FileResponse(STATIC_DIR / "logo.png", media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.api_route("/static/og-card.jpg", methods=["GET", "HEAD"])
    def og_card_jpg():
        """The 1200x630 link-preview card (og:image / twitter:image).
        Never shown on a page: only Facebook, X, LinkedIn, Slack, iMessage
        and the like fetch it when someone shares an Axionory link."""
        return FileResponse(STATIC_DIR / "og-card.jpg", media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.api_route("/static/{name}.webp", methods=["GET", "HEAD"])
    def dashboard_preview_webp(name: str):
        """The landing hero's dashboard image (two fixed sizes only)."""
        from fastapi import HTTPException
        if name not in ("dashboard-preview", "dashboard-preview-900"):
            raise HTTPException(404, "not found")
        return FileResponse(STATIC_DIR / f"{name}.webp", media_type="image/webp",
                            headers={"Cache-Control": "public, max-age=86400"})
