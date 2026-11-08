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
/* Ventoryn app bar: navy, full width, sticky — the same bar as the
   public landing nav. Brand centered, hamburger far right; the 44px
   spacer on the left balances the burger. One navigation for every
   signed-in page. Colors come from dashboard.ui tokens. */
.v-header { position: sticky; top: 0; z-index: 40; width: 100%;
  margin: 0; background: var(--v-navy, #0b0e1e);
  box-shadow: 0 1px 0 rgba(255, 255, 255, .06), 0 6px 20px rgba(11, 14, 30, .18); }
.v-bar { position: relative; max-width: 1120px; margin: 0 auto;
  padding: 8px 20px; min-height: 60px; display: flex; align-items: center;
  gap: 12px; }
.v-burger { flex: 0 0 44px; width: 44px; height: 44px; min-height: 44px;
  margin: 0; padding: 0; font-size: 20px; line-height: 1;
  border-radius: 10px; border: 1px solid rgba(255, 255, 255, .28);
  background: transparent; color: #fff; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center; }
.v-burger:hover, .v-burger[aria-expanded='true'] {
  background: rgba(255, 255, 255, .1); border-color: rgba(255, 255, 255, .5);
  color: #fff; }
.v-brand { flex: 1; text-align: center; font-size: 22px; font-weight: 800;
  color: #fff; text-decoration: none; letter-spacing: .02em; }
.v-brand::after { content: "."; color: var(--v-violet-line, #c4b5fd); }
.v-brand:hover { color: #fff; }
.v-bar-spacer { flex: 0 0 44px; width: 44px; }
.v-title { max-width: 1120px; margin: 26px auto 6px; padding: 0 20px;
  font-size: 26px; font-weight: 800; color: var(--v-ink, #1a1a2e);
  letter-spacing: -.01em; line-height: 1.25; }
.v-menu { position: absolute; right: 20px; top: calc(100% + 6px);
  z-index: 50; width: 300px; max-height: calc(100vh - 90px);
  overflow-y: auto; background: #fff; border: 1px solid var(--v-line, #e7e3f3);
  border-radius: 14px; box-shadow: 0 18px 48px rgba(11, 14, 30, .22);
  padding: 8px; }
.v-menu a { display: block; min-height: 44px; line-height: 44px;
  padding: 0 14px; border-radius: 8px; color: var(--v-ink, #1a1a2e);
  text-decoration: none; font-size: 15px; font-weight: 500; }
.v-menu a:hover { background: var(--v-violet-soft, #ede9fe);
  color: var(--v-violet-deep, #6d28d9); }
.v-current { display: block; min-height: 44px; line-height: 44px;
  padding: 0 14px; border-radius: 8px; color: var(--v-violet-deep, #6d28d9);
  font-size: 15px; font-weight: 700; background: var(--v-violet-soft, #ede9fe); }
.v-current em { font-style: normal; font-weight: 500; font-size: 13px;
  color: var(--v-muted, #5d5d78); }
.v-group { display: block; margin-top: 6px; padding: 12px 14px 4px;
  border-top: 1px solid var(--v-line, #e7e3f3); font-size: 11px;
  font-weight: 700; text-transform: uppercase; letter-spacing: .08em;
  color: var(--v-faint, #8b8ba3); }
@media (max-width: 560px) {
  .v-bar { padding: 8px 16px; min-height: 56px; }
  .v-title { font-size: 22px; margin-top: 20px; padding: 0 16px; }
  .v-menu { left: 12px; right: 12px; width: auto; }
}
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
        f"<nav class='v-menu' id='v-menu' aria-label='Ventoryn' hidden>"
        f"{items}{admin}</nav>"
        f"</div>"
        f"</header>{title_html}{HEADER_JS}")
