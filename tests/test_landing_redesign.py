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
- Vector only: no bitmap references anywhere in the rendered page.
"""

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
    assert 'class="preview"' in hero


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


def test_preview_light_panels_image2_style():
    r = _landing()
    # Dark mini sidebar + light main area (image 2's dashboard language).
    assert 'class="preview-side"' in r.text
    assert 'class="preview-main"' in r.text
    for item in ("Overview", "Campaigns", "Agents", "Approvals",
                 "Attribution", "Integrations", "Settings"):
        assert item in r.text
    assert "Last 30 Days" in r.text
    # Verified-customers cell: a donut (not a graph), plus revenue/profit —
    # matching how the real platform shows these, not a fake line chart.
    assert "1,000" in r.text and "verified" in r.text
    assert "$25,896.07" in r.text and "Revenue" in r.text
    assert "$3,006.87" in r.text and "Costs" in r.text
    assert "$22,889.20" in r.text and "Net profit" in r.text
    assert "Each one traced from an approved action to a Stripe checkout." in r.text
    assert "24 of 100" not in r.text
    assert "24 / 100" not in r.text
    assert "Goal: First 100" not in r.text
    assert "Progress ring: 24 of 100" not in r.text
    # Illustrative label, everywhere the numbers appear.
    assert "ILLUSTRATIVE DEMO DATA" in r.text
    assert "Illustrative product preview — not live customer data." in r.text


def test_preview_funnel_image1_numbers():
    r = _landing()
    for n in ("2,840", "186", "154", "4,320", "312"):
        assert n in r.text
    for label in ("Audience Opportunities", "Approved Actions",
                  "Published Actions", "Attributed Visits", "Signups",
                  "Verified Customers"):
        assert label in r.text


def test_preview_agents_queue_attribution():
    r = _landing()
    for agent in ("Audience Research Agent", "Social Listening Agent",
                  "Content Agent", "Attribution Agent"):
        assert agent in r.text
    assert r.text.count("Running") >= 4
    assert "Review proposed Reddit post" in r.text
    assert "Approve X engagement draft" in r.text
    assert "Review community outreach opportunity" in r.text


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
    # The preview grid must not blow out past the preview shell:
    # its tracks shrink below content width and its cells too.
    assert ("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)"
            in r.text)
    assert ".preview-cell {" in r.text and "min-width: 0" in r.text
    # The light preview panels sit inside the dark hero (white
    # text): the preview-main sets dark text, or the funnel
    # numbers, queue titles, agent names and trace labels inherit
    # white and are invisible on the light background.
    assert (".preview-main { flex: 1; min-width: 0; background: #f4f2fb;"
            " padding: 16px;" in r.text)
    assert "color: var(--ink); }" in r.text


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
    r = _landing()
    assert "$20/month" in r.text
    # The default monthly allowance (SAAS_MONTHLY_CREDITS overrides).
    assert "1,000 credits" in r.text
    for row in ("Analyze", "GEO audit", "Community listening",
                "Social report", "SEO article", "Video render"):
        assert row in r.text
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


def test_final_cta_footer_seo_vector_only():
    r = _landing()
    assert "Your Next Customer Is Out There." in r.text
    assert "Explore the Platform" in r.text
    assert "Account Login" in r.text
    assert "coming soon" in r.text.lower()
    # SEO: OG tags + JSON-LD, no og:image (bitmap — flagged for Spencer).
    assert 'property="og:title"' in r.text
    assert 'property="og:description"' in r.text
    assert 'property="og:image"' not in r.text
    assert "application/ld+json" in r.text
    assert '"price": "20"' in r.text
    # Vector only, except the brand logo/favicon (Spencer, 2026-11-12):
    # no OTHER bitmap reference, no other <img> on the page.
    text = r.text.replace('<img src="/static/logo.png" alt="Axionory">', ""
                          ).replace('<link rel="icon" type="image/png" href="/favicon.png">', "")
    for ext in (".jpg", ".jpeg", ".webp", ".gif"):
        assert ext not in text
    assert ".png" not in text
    assert "<img" not in text
    # No image-2 copy or concept.
    assert "multiple businesses" not in r.text.lower()
    assert "Get Started Free" not in r.text
    assert "revenue/expense" not in r.text.lower()
