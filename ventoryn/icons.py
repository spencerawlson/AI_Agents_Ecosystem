"""Inline SVG icons for the Axionory platform shell.

Every icon is a hand-drawn 24x24 stroke icon (``stroke="currentColor"``,
``fill="none"``, stroke-width 2, round caps/joins). No emoji, no font
glyphs, no bitmaps — the icon colour always comes from the surrounding
text colour, so the same icon works on the purple sidebar and on white
cards.
"""

from __future__ import annotations

# name -> inner SVG elements (paths/circles/rects inside a 24x24 viewBox).
_PATHS: dict[str, str] = {
    # home
    "overview": (
        '<path d="M3 10.5 12 3l9 7.5"/>'
        '<path d="M5 9.5V21h14V9.5"/>'
        '<path d="M9 21v-6h6v6"/>'
    ),
    # flag
    "campaigns": (
        '<path d="M5 21V4"/>'
        '<path d="M5 4h13l-2.5 4L18 12H5"/>'
    ),
    # sparkle
    "agents": (
        '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z"/>'
        '<path d="M18.5 15l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2z"/>'
    ),
    # check-square
    "approvals": (
        '<rect x="3" y="3" width="18" height="18" rx="3"/>'
        '<path d="M8 12.5l3 3 5.5-6.5"/>'
    ),
    # target
    "attribution": (
        '<circle cx="12" cy="12" r="9"/>'
        '<circle cx="12" cy="12" r="5"/>'
        '<circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none"/>'
    ),
    # bar chart
    "analytics": (
        '<path d="M3 21h18"/>'
        '<path d="M7 21v-6"/>'
        '<path d="M12 21V7"/>'
        '<path d="M17 21v-9"/>'
    ),
    # plug
    "integrations": (
        '<path d="M9 3v5"/>'
        '<path d="M15 3v5"/>'
        '<path d="M7 8h10v4a5 5 0 0 1-10 0V8z"/>'
        '<path d="M12 17v4"/>'
    ),
    # users
    "customers": (
        '<circle cx="9" cy="8" r="3.5"/>'
        '<path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5"/>'
        '<path d="M16 5.5a3.5 3.5 0 0 1 0 5.5"/>'
        '<path d="M17.5 14.7c2 .7 3.5 2.5 3.5 5.3"/>'
    ),
    # gear
    "settings": (
        '<circle cx="12" cy="12" r="3.2"/>'
        '<path d="M12 2.8v3M12 18.2v3M2.8 12h3M18.2 12h3"/>'
        '<path d="M5.5 5.5l2.1 2.1M16.4 16.4l2.1 2.1M18.5 5.5l-2.1 2.1M7.6 16.4l-2.1 2.1"/>'
    ),
    # shield with check
    "admin": (
        '<path d="M12 3l7 3v5c0 5-3.5 8-7 9-3.5-1-7-4-7-9V6l7-3z"/>'
        '<path d="M9 12l2 2 4-4.5"/>'
    ),
    "arrow_right": '<path d="M4 12h16"/><path d="M14 6l6 6-6 6"/>',
    "arrow_left": '<path d="M20 12H4"/><path d="M10 6l-6 6 6 6"/>',
    "check": '<path d="M4 12.5l5 5L20 6.5"/>',
    "search": (
        '<circle cx="11" cy="11" r="7"/>'
        '<path d="M16.5 16.5L21 21"/>'
    ),
    "shield": (
        '<path d="M12 3l7 3v5c0 5-3.5 8-7 9-3.5-1-7-4-7-9V6l7-3z"/>'
    ),
    "target": (
        '<circle cx="12" cy="12" r="9"/>'
        '<circle cx="12" cy="12" r="5"/>'
        '<circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none"/>'
    ),
    "lock": (
        '<rect x="5" y="11" width="14" height="9" rx="2"/>'
        '<path d="M8 11V7.5a4 4 0 0 1 8 0V11"/>'
    ),
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    # eye (revenue shown) / eye with a slash (revenue hidden)
    "eye": (
        '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/>'
        '<circle cx="12" cy="12" r="3"/>'
    ),
    "eye_off": (
        '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/>'
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M3 3l18 18"/>'
    ),
    "bell": (
        '<path d="M18 9a6 6 0 1 0-12 0c0 5-2 6-2 6h16s-2-1-2-6"/>'
        '<path d="M10 20a2.2 2.2 0 0 0 4 0"/>'
    ),
    "user": (
        '<circle cx="12" cy="8" r="4"/>'
        '<path d="M4.5 21c.8-3.8 3.8-6 7.5-6s6.7 2.2 7.5 6"/>'
    ),
    "globe": (
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M3 12h18"/>'
        '<path d="M12 3c2.8 2.6 4 5.7 4 9s-1.2 6.4-4 9c-2.8-2.6-4-5.7-4-9s1.2-6.4 4-9z"/>'
    ),
    # Websites (sidebar): the same globe as "globe" — a site's
    # world. "my-account" is the user icon under its sidebar name.
    "websites": (
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M3 12h18"/>'
        '<path d="M12 3c2.8 2.6 4 5.7 4 9s-1.2 6.4-4 9c-2.8-2.6-4-5.7-4-9s1.2-6.4 4-9z"/>'
    ),
    "my-account": (
        '<circle cx="12" cy="8" r="4"/>'
        '<path d="M4.5 21c.8-3.8 3.8-6 7.5-6s6.7 2.2 7.5 6"/>'
    ),
    "chevron_down": '<path d="M6 9.5l6 6 6-6"/>',
    "play": '<path d="M8 5.5v13l11-6.5-11-6.5z"/>',
    # Pause (the Overview header pill): two bars. "play" is reused
    # for the pill's resume state, so the narrow-screen icon alone
    # still says which way the toggle goes.
    "pause": '<path d="M9 5v14M15 5v14"/>',
    "layers": (
        '<path d="M12 3l9 5-9 5-9-5 9-5z"/>'
        '<path d="M3 13l9 5 9-5"/>'
    ),
}

#: Every icon name this module provides.
ICON_NAMES = tuple(_PATHS)


def icon(name: str, size: int = 18) -> str:
    """An inline SVG string for ``name`` (24x24 viewBox).

    Raises ValueError for an unknown name — callers use only the
    names in ICON_NAMES, so a typo fails loudly instead of rendering
    a silent blank.
    """
    try:
        inner = _PATHS[name]
    except KeyError:
        raise ValueError(f"unknown icon: {name!r}") from None
    return (
        f'<svg width="{int(size)}" height="{int(size)}" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
        f'focusable="false">{inner}</svg>'
    )
