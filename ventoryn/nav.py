"""The Ventoryn platform shell: purple sidebar + app header.

This replaces the old hamburger header. Every signed-in page renders
inside the platform shell (``ventoryn.ui.page`` with ``sidebar=`` +
``page_head=``):

- ``sidebar_html`` — the purple navigation sidebar (brand, the 9
  platform sections, the Growth Plan card, the operator-only Admin
  group, Sign out at the bottom).
- ``platform_head_html`` — the app header (mobile sidebar toggle,
  page title + subtitle, approvals bell, avatar dropdown).
- ``SIDEBAR_CSS`` / ``SIDEBAR_JS`` — the shell styles + behaviour.

Admin pages render through the same platform shell (the sidebar
with the Admin group active); the old admin-only centered header
(``simple_header_html`` / ``ADMIN_HEADER_CSS``) is gone. Only
``/admin/login`` stays bare — a login page with no nav is correct.

Rules (Spencer's):
- The page you are already on is NOT a clickable option: it renders
  as a non-clickable ``<span class="nav-item active">`` with
  ``aria-current="page"``, never a dead link.
- No emoji, no font glyphs, no bitmaps — icons are inline SVG from
  ``ventoryn.icons`` only.

Circular-import note: this module imports only ``ventoryn.icons``
(and stdlib ``html``). It never imports ``ventoryn.ui``. ``ui.page``
imports ``SIDEBAR_CSS``/``SIDEBAR_JS`` from here lazily, inside the
function, so there is no import cycle in either direction.

The old hamburger header (``HEADER_CSS`` / ``HEADER_JS`` /
``header_html``) is gone: its last callers (the operator
mission-report page and the account pages' non-sidebar branch)
now use the platform shell above. The admin-only centered header
(``simple_header_html`` / ``ADMIN_HEADER_CSS``) is gone too: admin
pages use the same platform shell, with the Admin group active.
"""

from __future__ import annotations

import html

from ventoryn.icons import icon

# (key, label, href) — the platform sections, in order. The order
# a customer reads the product: Overview first, then the thing they
# own (Websites), then the work (Campaigns, Approvals, Agents), then
# the proof (Analytics — which absorbed Attribution; /report stays
# as a deep link, not a menu item), then Customers and Integrations,
# then My account. No route moves: only labels and order change.
PLATFORM_ITEMS = (
    ("overview", "Overview", "/ops"),
    ("websites", "Websites", "/portfolio"),
    ("campaigns", "Campaigns", "/campaigns"),
    ("approvals", "Approvals", "/approvals"),
    ("agents", "Agents", "/agents"),
    ("analytics", "Analytics", "/analytics"),
    ("customers", "Customers", "/customers"),
    ("integrations", "Integrations", "/integrations"),
    ("my-account", "My account", "/account/dashboard"),
)

# The Admin group, operator only. Hrefs kept exactly as the old
# hamburger menu had them: /admin is the live feed; the other three
# are the oversight pages. "Agent activity" (not "Agents"): the
# customer Agents page is the roster; this is the operator's view
# of what the agents are doing across accounts.
_ADMIN_ITEMS = (
    ("admin-live", "Live", "/admin"),
    ("admin-subscribers", "Subscribers", "/admin/subscribers"),
    ("admin-accounts", "Accounts", "/admin/accounts"),
    ("admin-agents", "Agent activity", "/admin/agents"),
)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------------------
# Platform shell CSS + JS (adapted from the UI reference:
# white workspace, purple navigation sidebar, app header, cards).
# Desktop (>=1041px): the sidebar is always visible in the grid.
# Mobile (<=1040px): it is off-canvas, opened by the toggle (body
# class ``sidebar-open``), with a tap-to-close overlay.
# ---------------------------------------------------------------------------

SIDEBAR_CSS = """
/* Ventoryn platform shell: navy sidebar + tinted workspace — the same
   navy / violet as the landing page and its product preview. */
.app-layout { display: grid; grid-template-columns: 252px minmax(0, 1fr);
  min-height: 100vh; width: 100%; background: var(--v-bg, #f6f5fb); }
.app-sidebar { background: linear-gradient(180deg, #0b0e1e 0%, #151a33 100%);
  border-right: 1px solid rgba(255, 255, 255, .06);
  color: #fff; padding: 24px 13px; display: flex; flex-direction: column;
  gap: 27px; position: sticky; top: 0; height: 100vh; overflow-y: auto; }
.sidebar-brand { display: inline-flex; align-items: center; gap: 12px;
  font-size: 27px; font-weight: 800; letter-spacing: -1.2px; color: #fff;
  text-decoration: none; white-space: nowrap; padding: 0 17px; }
.sidebar-brand .brand-mark { display: inline-block; font-weight: 1000;
  font-size: 40px; line-height: .8; letter-spacing: -8px; padding-right: 9px;
  background: linear-gradient(135deg, #c4b5fd 8%, #8b5cf6 55%, #3b82f6);
  color: transparent; background-clip: text; transform: skew(-8deg); }
.sidebar-brand:hover { color: #fff; }
.app-nav { display: flex; flex-direction: column; gap: 4px; }
.app-nav .nav-item { position: relative; display: flex; align-items: center;
  gap: 17px; width: 100%; background: transparent; color: #fff; border: 0;
  border-radius: 10px; text-align: left; padding: 14px 20px; font-size: 15px;
  font-weight: 500; text-decoration: none; cursor: pointer; }
.app-nav .nav-item { color: #d9d4f5; }
.app-nav a.nav-item:hover { background: rgba(255, 255, 255, .07); color: #fff; }
.app-nav .nav-item.active { color: #fff;
  background: linear-gradient(100deg, #6d28d9, #7c3aed);
  box-shadow: 0 6px 18px rgba(109, 40, 217, .35); }
.app-nav .nav-item.active { font-weight: 700; cursor: default; }
.app-nav .nav-item svg { flex: 0 0 auto; width: 18px; height: 18px; }
.app-nav .nav-item .nav-label { flex: 1; min-width: 0; }
.nav-count { margin-left: auto; background: #8b5cf6; color: #fff;
  border-radius: 22px; font-size: 13px; font-weight: 700; padding: 4px 8px;
  line-height: 1; }
.nav-group { display: block; margin-top: 10px; padding: 12px 20px 4px;
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .08em; color: rgba(196, 181, 253, .7); }
.growth-plan { border: 1px solid rgba(196, 181, 253, .3);
  background: rgba(255, 255, 255, .04);
  border-radius: 15px; padding: 17px; margin-top: auto; }
.growth-title { font-size: 18px; font-weight: 800; }
.growth-plan p { font-size: 14px; margin: 13px 0 10px; }
.growth-progress { height: 10px; border: 0;
  border-radius: 100px; overflow: hidden; background: rgba(255, 255, 255, .1); }
.growth-progress i { display: block; height: 100%;
  background: linear-gradient(90deg, #8b5cf6, #c4b5fd); border-radius: inherit; }
.growth-plan strong { display: block; text-align: right; margin: 8px 3px 17px; }
.growth-plan .growth-owner { text-align: center; margin: 8px 0 0;
  font-size: 13px; color: #c4b5fd; }
.ghost-button { display: block; border: 1px solid rgba(255, 255, 255, .28); background: transparent;
  color: #fff; border-radius: 8px; padding: 11px; width: 100%;
  text-align: center; text-decoration: none; font-weight: 600; font-size: 14px; }
.ghost-button:hover { background: rgba(255, 255, 255, .1); color: #fff; }
.sidebar-foot { margin-top: auto; }
.sidebar-foot .nav-item { border-top: 1px solid rgba(255, 255, 255, .1);
  display: flex; align-items: center; gap: 17px; padding: 14px 20px;
  color: #d9d4f5; text-decoration: none; font-size: 15px; font-weight: 500;
  border-radius: 0 0 10px 10px; }
.sidebar-foot .nav-item:hover { color: #fff; background: rgba(255, 255, 255, .07); }
.sidebar-foot .nav-item svg { flex: 0 0 auto; width: 18px; height: 18px; }
.sidebar-account { font-size: 12px; color: rgba(255, 255, 255, .7);
  padding: 10px 20px 0; margin: 0; overflow-wrap: anywhere; }
.sidebar-overlay { display: none; }
.app-content { min-width: 0; background: var(--v-bg, #f6f5fb); }
.app-header { min-height: 83px; display: flex; align-items: center;
  justify-content: space-between; gap: 20px; padding: 12px 28px; }
.app-page-head { display: flex; align-items: center; gap: 16px; min-width: 0; }
.app-header h1 { font-size: 28px; letter-spacing: -1px; line-height: 1.1; margin: 0; }
.app-header p { margin: 5px 0 0; font-size: 14px; color: #2d2944; }
.app-header-actions { display: flex; align-items: center; gap: 25px;
  flex: 0 0 auto; }
.sidebar-toggle { display: none; border: 0; background: none; color: #1f1938;
  width: 44px; height: 44px; min-height: 44px; padding: 0; cursor: pointer;
  align-items: center; justify-content: center; border-radius: 10px; }
.sidebar-toggle:hover { background: #f1ebff; }
.notification { position: relative; display: inline-flex; align-items: center;
  justify-content: center; width: 44px; height: 44px; border-radius: 50%;
  color: #1f1938; text-decoration: none; }
.notification:hover { background: #f1ebff; }
/* Revenue switch: a pill track with a white knob. Off = grey, knob
   left; on = green-to-blue gradient, knob slides right. */
.rev-toggle { display: inline-flex; align-items: center; gap: 10px; border: 0;
  background: none; padding: 6px 4px; cursor: pointer; font: inherit;
  font-size: 13px; font-weight: 700; color: #4b4566; border-radius: 999px; }
.rev-toggle[hidden] { display: none; }
.rev-track { position: relative; width: 46px; height: 26px; flex: 0 0 auto;
  border-radius: 999px; background: #d9d5e6;
  box-shadow: inset 0 1px 2px rgba(15, 12, 40, .15);
  transition: background .25s ease; }
.rev-knob { position: absolute; top: 3px; left: 3px; width: 20px; height: 20px;
  border-radius: 50%; background: #fff;
  box-shadow: 0 3px 8px rgba(15, 12, 40, .28);
  transition: transform .25s ease; }
.rev-toggle[aria-checked='true'] .rev-track {
  background: linear-gradient(135deg, #7ef0b8 0%, #5fb8c9 50%, #5b8def 100%); }
.rev-toggle[aria-checked='true'] .rev-knob { transform: translateX(20px); }
.rev-toggle:focus-visible { outline: 2px solid #7c3aed; outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { .rev-track, .rev-knob { transition: none; } }
@media (max-width: 560px) { .rev-toggle .rev-label { display: none; } }
.header-badge { position: absolute; top: 2px; right: 0; min-width: 18px;
  height: 18px; padding: 0 5px; border-radius: 999px; background: #7c3aed;
  color: #fff; font-size: 11px; font-weight: 700; line-height: 18px;
  text-align: center; }
/* The slim pause pill (Overview header, operator only): the same
   POST /api/pause as the full card, compact. On narrow screens the
   text hides and the icon alone carries it — pause icon while
   running, play icon while paused. */
.pause-pill-form { margin: 0; }
.pause-pill { display: inline-flex; align-items: center; gap: 8px;
  min-height: 44px; padding: 0 14px; border-radius: 999px;
  border: 1px solid var(--v-line, #e7e3f3); background: #fff;
  color: #1f1938; font-size: 14px; font-weight: 600; cursor: pointer;
  white-space: nowrap; }
.pause-pill:hover { background: #f1ebff; }
.pause-pill svg { width: 15px; height: 15px; display: block; }
.avatar-menu { position: relative; }
.avatar { background: #6d28d9; border: 0; border-radius: 50%; width: 41px;
  height: 41px; min-height: 41px; padding: 0; color: #fff; font-weight: 800;
  font-size: 13px; cursor: pointer; display: inline-flex; align-items: center;
  justify-content: center; }
.avatar-dropdown { position: absolute; right: 0; top: calc(100% + 8px);
  z-index: 50; min-width: 200px; background: #fff;
  border: 1px solid var(--v-line, #e7e3f3); border-radius: 14px;
  box-shadow: 0 18px 48px rgba(11, 14, 30, .22); padding: 8px; }
.avatar-dropdown a { display: block; min-height: 44px; line-height: 44px;
  padding: 0 14px; border-radius: 8px; color: var(--v-ink, #1a1a2e);
  text-decoration: none; font-size: 15px; font-weight: 500; }
.avatar-dropdown a:hover { background: var(--v-violet-soft, #ede9fe);
  color: var(--v-violet-deep, #6d28d9); }
.app-main { padding: 0 26px 32px; max-width: 1800px; margin: 0 auto; }
@media (max-width: 1040px) {
  .app-layout { display: block; }
  .app-sidebar { position: fixed; left: -270px; top: 0; width: 255px;
    height: 100vh; z-index: 60; transition: left .2s;
    box-shadow: 0 0 20px rgba(0, 0, 0, .2); }
  body.sidebar-open .app-sidebar { left: 0; }
  .sidebar-overlay { display: block; position: fixed; inset: 0; z-index: 55;
    background: rgba(11, 14, 30, .45); border: 0; padding: 0; cursor: pointer; }
  body:not(.sidebar-open) .sidebar-overlay { display: none; }
  .sidebar-toggle { display: inline-flex; }
  .app-header { padding: 13px 18px; align-items: flex-start; }
  .app-header h1 { font-size: 22px; }
  .app-header p { font-size: 12px; }
  .app-header-actions { gap: 9px; }
  .app-main { padding: 0 14px 22px; }
}
@media (max-width: 560px) {
  .avatar { width: 32px; height: 32px; min-height: 32px; }
  .pause-pill span { display: none; }
  .pause-pill { padding: 0 10px; }
}
@media (prefers-reduced-motion: reduce) {
  .app-sidebar { transition: none; }
}
"""

SIDEBAR_JS = """
<script>
(function () {
  var toggle = document.querySelector('.sidebar-toggle');
  var overlay = document.querySelector('.sidebar-overlay');
  var avatarBtn = document.querySelector('.avatar-menu .avatar');
  var dropdown = document.querySelector('.avatar-dropdown');
  function closeSidebar() {
    document.body.classList.remove('sidebar-open');
    if (toggle) toggle.setAttribute('aria-expanded', 'false');
  }
  function closeDropdown() {
    if (dropdown) dropdown.hidden = true;
    if (avatarBtn) avatarBtn.setAttribute('aria-expanded', 'false');
  }
  if (toggle) {
    toggle.addEventListener('click', function (ev) {
      ev.stopPropagation();
      var open = document.body.classList.toggle('sidebar-open');
      toggle.setAttribute('aria-expanded', String(open));
    });
  }
  if (overlay) overlay.addEventListener('click', closeSidebar);
  if (avatarBtn && dropdown) {
    avatarBtn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      var open = dropdown.hidden;
      dropdown.hidden = !open;
      avatarBtn.setAttribute('aria-expanded', String(open));
    });
  }
  document.addEventListener('click', function (ev) {
    if (dropdown && !dropdown.hidden && avatarBtn &&
        !avatarBtn.contains(ev.target) && !dropdown.contains(ev.target)) {
      closeDropdown();
    }
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') { closeSidebar(); closeDropdown(); }
  });
})();
</script>
"""


def _nav_item_html(key: str, label: str, href: str,
                   current: str | None,
                   badge: int = 0, icon_name: str | None = None) -> str:
    """One sidebar item: a link, or a non-clickable span when it is
    the current page. ``badge`` (Approvals) shows the pending count
    when it is above zero. ``icon_name`` defaults to ``key`` (the
    platform keys are all icon names); admin items pass their own."""
    badge_html = (f"<b class='nav-count'>{badge}</b>"
                  if badge > 0 else "")
    inner = (f"{icon(icon_name or key)}<span class='nav-label'>"
             f"{esc(label)}</span>{badge_html}")
    if key == current:
        return (f"<span class='nav-item active' aria-current='page'>"
                f"{inner}</span>")
    return f"<a class='nav-item' href='{esc(href)}'>{inner}</a>"


def sidebar_html(current: str | None, is_operator: bool, *,
                 verified: int = 0, pending_approvals: int = 0,
                 account_email: str = "") -> str:
    """The platform sidebar (``<aside class="app-sidebar">``) plus
    the mobile tap-to-close overlay.

    ``current`` is the PLATFORM_ITEMS key of the page being rendered
    (or None when the page is not a sidebar item): that item renders
    as a non-clickable span, never a dead link. ``verified`` is the
    account's verified-customer count.
    ``pending_approvals`` badges the Approvals item (and the header
    bell) when above zero. The operator sees the Admin group and
    "Owner account — permanent" instead of the Manage Plan button.
    """
    verified = max(0, int(verified))
    pct = min(verified, 100)
    items = "".join(
        _nav_item_html(key, label, href, current,
                       badge=(pending_approvals if key == "approvals"
                              else 0))
        for key, label, href in PLATFORM_ITEMS)
    if is_operator:
        growth_action = ("<p class='growth-owner'>Owner account — "
                         "permanent</p>")
        admin = ("<span class='nav-group'>Admin</span>" + "".join(
            _nav_item_html(key, label, href, current,
                           icon_name=icon_name)
            for (key, label, href), icon_name in zip(
                _ADMIN_ITEMS,
                ("admin", "customers", "user", "agents"))))
    else:
        growth_action = ("<a class='ghost-button' "
                         "href='/account/subscribe'>Manage Plan</a>")
        admin = ""
    email_html = (f"<p class='sidebar-account'>{esc(account_email)}</p>"
                  if account_email else "")
    return (
        f"<aside class='app-sidebar' id='app-sidebar'>"
        f"<a class='sidebar-brand' href='/ops'>"
        f"<span class='brand-mark'>V</span> Ventoryn</a>"
        f"<nav class='app-nav' aria-label='Platform'>{items}{admin}</nav>"
        f"<div class='growth-plan'>"
        f"<div class='growth-title'>Growth Plan</div>"
        f"<p>Verified customers</p>"
        f"<div class='growth-progress'><i style='width:{pct}%'></i></div>"
        f"<strong>{verified} verified</strong>"
        f"{growth_action}</div>"
        f"<div class='sidebar-foot'>"
        f"<a class='nav-item' href='/account/logout'>"
        f"{icon('arrow_right')}<span class='nav-label'>Sign out</span></a>"
        f"{email_html}</div>"
        f"</aside>"
        f"<div class='sidebar-overlay' aria-hidden='true'></div>")


REVENUE_TOGGLE_JS = "<script>document.addEventListener('DOMContentLoaded',function(){var b=document.getElementById('rev-toggle');if(!b)return;var cells=document.querySelectorAll('[data-rev-total],[data-rev-profit],[data-rev-site],[data-rev-site-profit]');if(!cells.length)return;b.hidden=false;var KEY='ventoryn.revealRevenue',shown=false;cells.forEach(function(el){el.setAttribute('data-masked',el.textContent);});function mark(on){shown=on;b.setAttribute('aria-checked',on?'true':'false');var t=on?'Hide revenue':'Show revenue';b.setAttribute('aria-label',t);b.title=t;try{on?sessionStorage.setItem(KEY,'1'):sessionStorage.removeItem(KEY);}catch(e){}}function hide(){cells.forEach(function(el){el.textContent=el.getAttribute('data-masked');});mark(false);}function show(){fetch('/api/websites/revenue',{credentials:'same-origin',cache:'no-store'}).then(function(r){if(!r.ok)throw r;return r.json();}).then(function(d){document.querySelectorAll('[data-rev-total]').forEach(function(el){el.textContent=d.total;});document.querySelectorAll('[data-rev-profit]').forEach(function(el){el.textContent=d.net_profit;});document.querySelectorAll('[data-rev-site]').forEach(function(el){var v=d.sites[el.getAttribute('data-rev-site')];if(v)el.textContent=v;});document.querySelectorAll('[data-rev-site-profit]').forEach(function(el){var v=(d.site_profit||{})[el.getAttribute('data-rev-site-profit')];if(v)el.textContent=v;});mark(true);}).catch(function(){});}b.addEventListener('click',function(){shown?hide():show();});var keep=false;try{keep=sessionStorage.getItem(KEY)==='1';}catch(e){}if(keep)show();});</script>"


def platform_head_html(title: str, subtitle: str = "", *,
                       pending_approvals: int = 0,
                       pause_pill: str = "") -> str:
    """The app header: mobile sidebar toggle, page title + subtitle,
    approvals bell (badged when there are pending approvals), and an
    avatar button ("V") opening a dropdown with My account /
    Subscribe / Sign out. ``pause_pill`` is the operator-only slim
    pause control (Overview only) — it renders before the bell."""
    subtitle_html = (f"<p>{esc(subtitle)}</p>" if subtitle else "")
    badge_html = (f"<b class='header-badge'>{int(pending_approvals)}</b>"
                  if pending_approvals > 0 else "")
    return (
        f"<header class='app-header'>"
        f"<div class='app-page-head'>"
        f"<button class='sidebar-toggle' type='button' aria-expanded='false' "
        f"aria-controls='app-sidebar' aria-label='Open menu'>"
        f"{icon('menu', 23)}</button>"
        f"<div><h1>{esc(title)}</h1>{subtitle_html}</div>"
        f"</div>"
        f"<div class='app-header-actions'>"
        f"{pause_pill}"
        # Revenue show/hide: one toggle for the whole page. Hidden
        # unless the page has hidden revenue to reveal; a click fetches
        # the owner's figures, another hides them (see the script).
        f"<button class='rev-toggle' id='rev-toggle' type='button' role='switch' "
        f"hidden aria-checked='false' aria-label='Show revenue' title='Show revenue'>"
        f"<span class='rev-label'>Revenue</span>"
        f"<span class='rev-track' aria-hidden='true'><span class='rev-knob'></span></span>"
        f"</button>"
        f"<a class='notification' href='/approvals' aria-label='Approvals'>"
        f"{icon('bell', 23)}{badge_html}</a>"
        f"<div class='avatar-menu'>"
        f"<button class='avatar' type='button' aria-haspopup='true' "
        f"aria-expanded='false' aria-label='Account menu'>V</button>"
        f"<div class='avatar-dropdown' hidden>"
        f"<a href='/account/dashboard'>My account</a>"
        f"<a href='/account/subscribe'>Subscribe</a>"
        f"<a href='/account/logout'>Sign out</a>"
        f"</div></div>"
        f"</div>"
        f"</header>" + REVENUE_TOGGLE_JS)
