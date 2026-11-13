"""Landing-page redesign per the consolidated brief + the corrected
reference-image reading (2026-11-08).

- Images 1 + 2 are both the landing page: WORDING from image 1
  ("Become an Axionory Founding Customer", "Let AI Find Them. You Approve
  Every Move.", the five steps, the trust statements); DASHBOARD
  PREVIEW VISUAL from image 2 (light, clean dashboard panels with
  charts and tables) — the hero keeps image 1's navy background and
  copy, the preview inside it is rebuilt in image 2's light-panel
  style with Axionory's acquisition content (verified-customer
  summary, funnel, agents, approval queue, attribution), NOT image 2's
  multi-business content.
- Image 3 is the attribution-first repositioning (report pages).
- Vector only: no bitmap references anywhere in the rendered page,
  except the real dashboard screenshot (Spencer, 2026-11-12).
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from test_landing import _app_with


def _landing():
    app, _store = _app_with()
    return TestClient(app).get("/")


def test_hero_copy_exact():
    r = _landing()
    assert r.status_code == 200
    assert "AI-Powered Customer Acquisition" in r.text
    assert "Become an Axionory" in r.text and "Founding Customer" in r.text
    # The program name is gradient text (image 1's "100" was).
    assert 'class="grad">Founding Customer</span>' in r.text
    assert "Let AI Find Them. You Approve Every Move." in r.text
    assert "Enter your website, connect your accounts" in r.text
    assert "Start Getting Customers" in r.text
    assert "See How It Works" in r.text
    for trust in ("Human-approved actions",
                  "Verified customer attribution",
                  "Revenue privacy controls"):
        assert trust in r.text
    assert "Sign in with Google" in r.text


def test_hero_is_two_column_with_preview_inside():
    r = _landing()
    assert 'class="wrap hero-grid"' in r.text
    # The preview lives inside the hero now (no separate #preview section).
    assert 'id="preview"' not in r.text
    assert "See what Axionory sees" not in r.text
    hero = r.text.split('<section class="hero"')[1].split("</section>")[0]
    assert 'class="preview-shot"' in hero


def test_public_nav_with_product_dropdown():
    r = _landing()
    for link in ("#product", "#how-it-works", "#attribution",
                 "#pricing", "#faq"):
        assert f'href="{link}"' in r.text
    assert 'id="nav-drop-btn"' in r.text
    assert "Product" in r.text
    assert "Dashboard preview" in r.text
    assert "Who it" in r.text  # dropdown item "Who it's for"
    assert "Sign In" in r.text and "Get Started" in r.text
    assert 'id="nav-toggle"' in r.text  # mobile nav toggle
    # Spencer 2026-11-08: the mobile hamburger sits at the FAR LEFT
    # of the header, before the brand — never centered between the
    # brand and the Get Started button.
    assert (r.text.index('id="nav-toggle"')
            < r.text.index('class="brand"'))


def test_preview_is_real_dashboard_screenshot():
    # Spencer 2026-11-12: the HTML mockup is gone; the hero shows a
    # real screenshot of the Axionory dashboard (real numbers), so the
    # illustrative-demo labels are gone with it.
    r = _landing()
    hero = r.text.split('<section class="hero"')[1].split("</section>")[0]
    assert 'class="preview-shot"' in hero
    assert 'src="/static/dashboard-preview.webp"' in hero
    assert "/static/dashboard-preview-900.webp 900w" in hero    # phones get the small one
    assert 'width="1528" height="831"' in hero                  # no layout shift
    assert ('alt="The Axionory dashboard: $25,896.07 revenue, 1,004 verified '
            'customers, and your websites."' in hero)
    assert "ILLUSTRATIVE DEMO DATA" not in r.text
    assert "Illustrative product preview" not in r.text
    assert "not live customer data" not in r.text
    assert "not real customer data" not in r.text
    # No mockup markup survives anywhere on the page.
    for gone in ('class="preview-side"', 'class="preview-main"',
                 'class="preview-grid"', 'class="preview-cell"',
                 'class="demo-note"', 'class="preview-bar"',
                 'class="preview-body"'):
        assert gone not in r.text
    # The screenshot is served by the static route.
    app, _store = _app_with()
    c = TestClient(app)
    for name in ("dashboard-preview", "dashboard-preview-900"):
        img = c.get(f"/static/{name}.webp")
        assert img.status_code == 200 and img.headers["content-type"] == "image/webp"
        assert img.content[8:12] == b"WEBP"
    assert c.get("/static/other.webp").status_code == 404
    # Top edge level with the subheadline (Spencer 2026-11-12): measured
    # on the 1920px render, the subheadline's first glyph sits 183px
    # below the hero content top (subheadline y 299, image top y 116),
    # so the preview drops by exactly that. The offset is reset where
    # the hero stacks (max-width 900px) so the image sits cleanly below
    # the analyze card with no stray gap.
    assert "margin-top: 183px;" in r.text
    assert ".preview-shot { margin-top: 0; }" in r.text


def test_how_it_works_image1():
    r = _landing()
    assert "Axionory" in r.text and "Works" in r.text
    assert "From your website to verified paying customers" in r.text
    assert "in 5 simple steps." in r.text
    for step in ("Enter Your Website", "Connect Your Accounts",
                 "Let AI Research Opportunities", "Approve Every Action",
                 "Track Verified Customers"):
        assert step in r.text
    assert r.text.count('class="step-icon"') == 5
    assert "Join the Founding Customer Waitlist" in r.text
    assert 'href="/founding-customer-waitlist"' in r.text
    # Step connectors are inline SVG arrows between the cards — the
    # old font-glyph arrow rendered as "92" here. They exist on
    # desktop and are hidden where the cards stack.
    assert ".steps li:not(:last-child)::after" in r.text
    assert "data:image/svg+xml" in r.text
    # The screenshot must not blow out past the hero: it shrinks
    # below content width (max-width 100% + min-width 0).
    assert ".preview-shot {" in r.text and "min-width: 0" in r.text


def test_features_attribution_differentiation_who():
    r = _landing()
    for f in ("AI Audience Discovery", "Smart Social Engagement",
              "Social Listening", "Approval-First Automation",
              "Verified Conversion Attribution", "Revenue Privacy"):
        assert f in r.text
    assert "Not Just Clicks. Real Paying Customers." in r.text
    for step in ("AI Recommendation", "Customer Approval",
                 "Signed Campaign Link", "Website Visit", "Stripe Payment",
                 "Verified Conversion"):
        assert step in r.text
    assert "AI Does the Research. You Make the Decisions." in r.text
    for who in ("SaaS founders", "Independent software developers",
                "Startup teams", "Online service businesses",
                "Digital product creators",
                "Founders launching their first website"):
        assert who in r.text


def test_pricing_from_verified_stripe_data():
    # The three plans, straight from ventoryn/plans.py (Spencer, 2026-11-13):
    # the old $20 credits card is gone (its Stripe price is not sold).
    r = _landing()
    for label, price in (("Starter", 20), ("Growth", 39), ("Pro", 99)):
        assert f"<h3>{label}</h3>" in r.text
        assert f'<span class="big">${price}</span> / month' in r.text
    for plan in ("starter", "growth", "pro"):
        assert f'href="/account/subscribe#plan-{plan}"' in r.text
    assert "Posts to X<" in r.text and "Posts to X, Facebook and Instagram<" in r.text
    assert "Most popular" in r.text
    assert "What a credit buys" not in r.text and "1,000 credits" not in r.text
    assert "No free trial" in r.text
    assert "No guarantees" in r.text


def test_faq_ten_items_with_2fa_in_ten():
    r = _landing()
    assert r.text.count("<details>") == 10
    for q in ("What does Axionory do?",
              "Will Axionory post without my approval?",
              "Which accounts can I connect?",
              "How does Axionory verify paying customers?",
              "Do I need Stripe?",
              "Can I hide my revenue?",
              "Does Axionory guarantee new customers?",
              "Can I disconnect my accounts?",
              "How is my information protected?"):
        assert q in r.text
    # FAQ #10 covers mandatory 2FA + zero-trust.
    assert "two-factor" in r.text.lower()
    assert "zero-trust" in r.text.lower()


def test_landing_is_wide_logo_far_left():
    # Spencer 2026-11-12 (wide monitor): the page was a narrow centered
    # column with big empty margins on both ends, the hero preview a
    # tall narrow box, and the logo inset with the container. The page
    # container is wide now, the hero wider still, and the header spans
    # the full viewport so the brand sits at the far left edge.
    r = _landing()
    assert ".wrap { max-width: 1440px; margin: 0 auto; padding: 0 20px; }" in r.text
    assert ".hero .wrap { max-width: 1680px; }" in r.text
    # The dashboard screenshot gets the larger share of the hero
    # (Spencer 2026-11-12: make it a bit larger): 4fr copy / 8fr preview,
    # so the image renders ~1064px wide at the 1680px wrap — just under
    # its 1102px source, so it stays crisp.
    assert ("grid-template-columns: minmax(0, 4fr) minmax(0, 8fr);" in r.text)
    assert (".site-nav .wrap { display: flex; align-items: center; gap: 16px;\n"
            "  min-height: 60px; max-width: none; padding: 0 24px; }" in r.text)
    # The nav links stay centred in the wide header: the <nav> wrapper
    # takes the free space between the far-left brand and the right-hand
    # actions, and the links centre inside it.
    assert (".nav-links { display: flex; gap: 4px; margin: 0; list-style: none;\n"
            "  padding: 0; flex: 1; justify-content: center; }" in r.text)
    assert (".site-nav nav { flex: 1; display: flex; justify-content: center; }"
            in r.text)
    # The brand is the first header item after the (mobile-only)
    # hamburger: far left, ahead of the nav links and the actions.
    assert (r.text.index('class="brand"')
            < r.text.index('class="nav-links"'))
    assert (r.text.index('class="brand"')
            < r.text.index('class="nav-actions"'))
    # The preview still lives inside the two-column hero.
    assert 'class="wrap hero-grid"' in r.text


def test_final_cta_footer_seo_vector_only():
    r = _landing()
    assert "Your Next Customer Is Out There." in r.text
    assert "Explore the Platform" in r.text
    assert "Account Login" in r.text
    assert "coming soon" in r.text.lower()
    # SEO: OG tags + JSON-LD + the link-preview card (Spencer, 2026-11-13).
    assert 'property="og:title"' in r.text
    assert 'property="og:description"' in r.text
    assert re.search(r'property="og:image" content="[^"]*/static/og-card\.jpg"', r.text)
    assert 'name="twitter:card" content="summary_large_image"' in r.text
    assert "application/ld+json" in r.text
    assert '"price": "20"' in r.text
    # Vector only, except the brand logo/favicon and the real dashboard
    # screenshot (Spencer, 2026-11-12): no OTHER bitmap reference, no
    # other <img> on the page.
    text = r.text.replace('<img src="/static/logo.png" alt="Axionory">', ""
                          ).replace('<link rel="icon" type="image/png" href="/favicon.png">', ""
                          )
    text = re.sub(r'<img class="preview-shot" src="/static/dashboard-preview\.webp"[^>]*>', "", text)
    # ...and the link-preview card, named only in two meta tags.
    text = re.sub(r'<meta (property="og:image"|name="twitter:image") content="[^"]*/static/og-card\.jpg">',
                  "", text)
    for ext in (".jpg", ".jpeg", ".webp", ".gif"):
        assert ext not in text
    assert ".png" not in text
    assert "<img" not in text
    # No image-2 copy or concept.
    assert "multiple businesses" not in r.text.lower()
    assert "Get Started Free" not in r.text
    assert "revenue/expense" not in r.text.lower()


def test_link_preview_card_is_served_at_1200x630():
    import io

    from PIL import Image

    app, _store = _app_with()
    r = TestClient(app).get("/static/og-card.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(r.content)).size == (1200, 630)
    assert len(r.content) < 300_000  # small enough for every preview crawler
