"""One shared header for every signed-in page.

Centered Ventoryn brand + page title, and a hamburger button that
opens the ONLY navigation. The old pill row of links (Live Ops / My
Portfolio / Approvals / Report / My account / Subscribe / Sign out)
spread across the page is gone — the menu holds every section.

Rules (Spencer's):
- The page you are already on is NOT a clickable option in the menu:
  it renders as a non-clickable "current page" label instead. A dead
  link is never shown.
- Customer menu: Live Ops, My Portfolio, Approvals, Report, My
  account, Subscribe, Sign out.
- Operator menu: the same, plus an Admin group (Live, Subscribers,
  Accounts, Agents).
- The landing page (public, unsigned) does not use this header.

The header is pure HTML + a few lines of vanilla JS (toggle, close
on tap-outside, close on Escape). No framework, no images.
"""

from __future__ import annotations

import html

# (key, label, href) — the customer menu, in order.
_CUSTOMER_ITEMS = (
    ("live-ops", "Live Ops", "/ops"),
    ("portfolio", "My Portfolio", "/portfolio"),
    ("approvals", "Approvals", "/approvals"),
    ("report", "Report", "/report"),
    ("account", "My account", "/account/dashboard"),
    ("subscribe", "Subscribe", "/account/subscribe"),
    ("signout", "Sign out", "/account/logout"),
)

# The Admin group, operator only. /admin is the live feed; the other
# three are the oversight pages.
_ADMIN_ITEMS = (
    ("admin-live", "Live", "/admin"),
    ("admin-subscribers", "Subscribers", "/admin/subscribers"),
    ("admin-accounts", "Accounts", "/admin/accounts"),
    ("admin-agents", "Agents", "/admin/agents"),
)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


HEADER_CSS = """
/* Ventoryn shared header: centered brand + page title, hamburger
   menu. One navigation for every signed-in page. */
.v-header { position: relative; width: 100%; margin: 0 auto 8px; }
.v-bar { display: flex; align-items: center; gap: 12px; }
/* Hamburger at the FAR RIGHT; the 44px spacer on the left balances
   it so the centered brand stays centered. */
.v-burger { flex: 0 0 44px; width: 44px; height: 44px; min-height: 44px;
  padding: 0; font-size: 22px; line-height: 1; border-radius: 10px;
  border: 1px solid #c4b5fd; background: #f3efff; color: #111;
  cursor: pointer; }
.v-burger:hover { background: #e9e2ff; }
.v-brand { flex: 1; text-align: center; font-size: 24px; font-weight: 900;
  color: #6d28d9; text-decoration: none; letter-spacing: .3px; }
.v-bar-spacer { flex: 0 0 44px; width: 44px; }
.v-title { text-align: center; font-size: 26px; font-weight: 800;
  margin: 10px 0 4px; color: #1a1a2e; }
.v-menu { position: absolute; left: 0; right: 0; top: 100%; z-index: 50;
  background: #fff; border: 1px solid #e5e0f5; border-radius: 12px;
  box-shadow: 0 8px 28px rgba(109, 40, 217, 0.16); padding: 8px;
  margin-top: 4px; }
.v-menu a { display: block; min-height: 44px; line-height: 44px;
  padding: 0 14px; border-radius: 8px; color: #1a1a2e;
  text-decoration: none; font-size: 16px; }
.v-menu a:hover { background: #f3efff; }
.v-current { display: block; min-height: 44px; line-height: 44px;
  padding: 0 14px; border-radius: 8px; color: #666; font-size: 16px;
  background: #faf8ff; }
.v-current em { font-style: normal; color: #6d28d9; font-weight: bold; }
.v-group { display: block; padding: 10px 14px 4px; font-size: 12px;
  font-weight: bold; text-transform: uppercase; letter-spacing: 1px;
  color: #888; }
"""

HEADER_JS = """
<script>
(function () {
  var burger = document.querySelector('.v-burger');
  var menu = document.querySelector('.v-menu');
  if (!burger || !menu) return;
  function closeMenu() {
    menu.hidden = true;
    burger.setAttribute('aria-expanded', 'false');
  }
  burger.addEventListener('click', function (ev) {
    ev.stopPropagation();
    var open = menu.hidden;
    menu.hidden = !open;
    burger.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('click', function (ev) {
    if (!menu.hidden && !menu.contains(ev.target)) closeMenu();
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') closeMenu();
  });
})();
</script>
"""


def _item_html(key: str, label: str, href: str, current: str | None) -> str:
    if key == current:
        return (f"<span class='v-current' aria-current='page'>{esc(label)}"
                f" <em>&middot; current page</em></span>")
    return f"<a href='{esc(href)}'>{esc(label)}</a>"


def header_html(current: str | None, is_operator: bool,
                title: str) -> str:
    """The shared header for a signed-in page.

    ``current`` is the key of the page being rendered (or None when
    the page is not a menu item — an error page, say): that item
    renders as a non-clickable label, never a dead link. An empty
    ``title`` renders no heading — the Live Ops page carries its own
    title in the HUD.
    """
    title_html = (f"<h1 class='v-title'>{esc(title)}</h1>"
                  if title else "")
    items = "".join(
        _item_html(key, label, href, current)
        for key, label, href in _CUSTOMER_ITEMS)
    admin = ""
    if is_operator:
        admin = ("<span class='v-group'>Admin</span>" + "".join(
            _item_html(key, label, href, current)
            for key, label, href in _ADMIN_ITEMS))
    return (
        f"<header class='v-header'>"
        f"<div class='v-bar'>"
        f"<span class='v-bar-spacer' aria-hidden='true'></span>"
        f"<a class='v-brand' href='/ops'>Ventoryn</a>"
        f"<button class='v-burger' type='button' aria-expanded='false' "
        f"aria-controls='v-menu' aria-label='Menu'>&#9776;</button>"
        f"</div>"
        f"{title_html}"
        f"<nav class='v-menu' id='v-menu' aria-label='Ventoryn' hidden>"
        f"{items}{admin}</nav>"
        f"</header>{HEADER_JS}")
