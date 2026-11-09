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

# (key, label, href) — the platform sections, in order.
PLATFORM_ITEMS = (
    ("overview", "Overview", "/ops"),
    ("campaigns", "Campaigns", "/campaigns"),
    ("agents", "Agents", "/agents"),
    ("approvals", "Approvals", "/approvals"),
    ("attribution", "Attribution", "/report"),
    ("analytics", "Analytics", "/analytics"),
    ("integrations", "Integrations", "/integrations"),
    ("customers", "Customers", "/customers"),
    ("settings", "Settings", "/account/dashboard"),
)

# The Admin group, operator only. Hrefs kept exactly as the old
# hamburger menu had them: /admin is the live feed; the other three
# are the oversight pages.
_ADMIN_ITEMS = (
    ("admin-live", "Live", "/admin"),
    ("admin-subscribers", "Subscribers", "/admin/subscribers"),
    ("admin-accounts", "Accounts", "/admin/accounts"),
    ("admin-agents", "Agents", "/admin/agents"),
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
/* Ventoryn platform shell: purple sidebar + white workspace.
   Adapted from the UI reference (platform.html / styles.css). */
.app-layout { display: grid; grid-template-columns: 252px minmax(0, 1fr);
  min-height: 100vh; width: 100%; background: #fff; }
.app-sidebar { background: linear-gradient(160deg, #8623ee, #5014a3 62%, #6a1fc0);
  color: #fff; padding: 24px 13px; display: flex; flex-direction: column;
  gap: 27px; position: sticky; top: 0; height: 100vh; overflow-y: auto; }
.sidebar-brand { display: inline-flex; align-items: center; gap: 12px;
  font-size: 27px; font-weight: 800; letter-spacing: -1.2px; color: #fff;
  text-decoration: none; white-space: nowrap; padding: 0 17px; }
.sidebar-brand .brand-mark { display: inline-block; font-weight: 1000;
  font-size: 40px; line-height: .8; letter-spacing: -8px; padding-right: 9px;
  background: linear-gradient(135deg, #da50fa 8%, #7b4bff 58%, #51a8fa);
  color: transparent; background-clip: text; transform: skew(-8deg); }
.sidebar-brand:hover { color: #fff; }
.app-nav { display: flex; flex-direction: column; gap: 4px; }
.app-nav .nav-item { position: relative; display: flex; align-items: center;
  gap: 17px; width: 100%; background: transparent; color: #fff; border: 0;
  border-radius: 10px; text-align: left; padding: 14px 20px; font-size: 15px;
  font-weight: 500; text-decoration: none; cursor: pointer; }
.app-nav a.nav-item:hover, .app-nav .nav-item.active {
  background: linear-gradient(100deg, #b344ff, #7e28f2);
  box-shadow: inset 0 0 1px rgba(255, 255, 255, .5); }
.app-nav .nav-item.active { font-weight: 700; cursor: default; }
.app-nav .nav-item svg { flex: 0 0 auto; width: 18px; height: 18px; }
.app-nav .nav-item .nav-label { flex: 1; min-width: 0; }
.nav-count { margin-left: auto; background: #b936f5; color: #fff;
  border-radius: 22px; font-size: 13px; font-weight: 700; padding: 4px 8px;
  line-height: 1; }
.nav-group { display: block; margin-top: 10px; padding: 12px 20px 4px;
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .08em; color: rgba(255, 255, 255, .65); }
.growth-plan { border: 1px solid #ad5bf9;
  background: linear-gradient(140deg, #7a26d6, #4a078e);
  border-radius: 15px; padding: 17px; margin-top: auto; }
.growth-title { font-size: 18px; font-weight: 800; }
.growth-plan p { font-size: 14px; margin: 13px 0 10px; }
.growth-progress { height: 14px; border: 1px solid #d7a0fd;
  border-radius: 100px; overflow: hidden; background: #57239a; }
.growth-progress i { display: block; height: 100%;
  background: linear-gradient(90deg, #e77bfe, #9c51fe); border-radius: inherit; }
.growth-plan strong { display: block; text-align: right; margin: 8px 3px 17px; }
.growth-plan .growth-owner { text-align: center; margin: 8px 0 0;
  font-size: 13px; color: #e9d5ff; }
.ghost-button { display: block; border: 1px solid #d4b5f6; background: transparent;
  color: #fff; border-radius: 8px; padding: 11px; width: 100%;
  text-align: center; text-decoration: none; font-weight: 600; font-size: 14px; }
.ghost-button:hover { background: rgba(255, 255, 255, .1); color: #fff; }
.sidebar-foot { margin-top: auto; }
.sidebar-foot .nav-item { border-top: 1px solid rgba(255, 255, 255, .25); }
.sidebar-account { font-size: 12px; color: rgba(255, 255, 255, .7);
  padding: 10px 20px 0; margin: 0; overflow-wrap: anywhere; }
.sidebar-overlay { display: none; }
.app-content { min-width: 0; background: #fff; }
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
.header-badge { position: absolute; top: 2px; right: 0; min-width: 18px;
  height: 18px; padding: 0 5px; border-radius: 999px; background: #9a2cf6;
  color: #fff; font-size: 11px; font-weight: 700; line-height: 18px;
  text-align: center; }
.avatar-menu { position: relative; }
.avatar { background: #7f36e7; border: 0; border-radius: 50%; width: 41px;
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
    account's verified-customer count toward the first 100.
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
        f"<p>Goal: First 100 Customers</p>"
        f"<div class='growth-progress'><i style='width:{pct}%'></i></div>"
        f"<strong>{verified} / 100</strong>"
        f"{growth_action}</div>"
        f"<div class='sidebar-foot'>"
        f"<a class='nav-item' href='/account/logout'>"
        f"{icon('arrow_right')}<span class='nav-label'>Sign out</span></a>"
        f"{email_html}</div>"
        f"</aside>"
        f"<div class='sidebar-overlay' aria-hidden='true'></div>")


def platform_head_html(title: str, subtitle: str = "", *,
                       pending_approvals: int = 0) -> str:
    """The app header: mobile sidebar toggle, page title + subtitle,
    approvals bell (badged when there are pending approvals), and an
    avatar button ("V") opening a dropdown with My account /
    Subscribe / Sign out."""
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
        f"</header>")
