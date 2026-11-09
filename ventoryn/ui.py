"""Ventoryn UI: one set of design tokens and one page shell.

Every server-rendered page (signed-in app pages, the SaaS account
pages, the admin board, the operator tools) is built with ``page()``
and styled by ``BASE_CSS``, so they all share the landing page's
brand: navy app bar, violet accent, white cards on a soft tint.
The professional Overview (Live Ops) is server-rendered through the
same shell — there is no separate game page any more.

Page-specific CSS (portfolio stat cards, report charts, admin board)
is layered on top through ``extra_css`` and should only use the
tokens defined here — no new hex colors.
"""

from __future__ import annotations

import html

TOKENS_CSS = """
:root {
  --v-navy: #0b0e1e; --v-navy-2: #151a33;
  --v-violet: #8b5cf6; --v-violet-deep: #6d28d9; --v-violet-soft: #ede9fe;
  --v-violet-line: #c4b5fd;
  --v-blue: #3b82f6; --v-blue-soft: #e0ecff;
  --v-green: #15803d; --v-green-soft: #dcfce7;
  --v-red: #dc2626; --v-red-soft: #fee2e2;
  --v-amber: #b45309; --v-amber-soft: #fef3c7;
  --v-ink: #1a1a2e; --v-muted: #5d5d78; --v-faint: #8b8ba3;
  --v-bg: #f6f5fb; --v-card: #ffffff; --v-line: #e7e3f3;
  --v-line-strong: #d6d0ea;
  --v-radius: 14px; --v-radius-sm: 10px;
  --v-shadow: 0 1px 2px rgba(26, 26, 46, .04), 0 4px 16px rgba(26, 26, 46, .05);
  --v-font: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
"""

BASE_CSS = TOKENS_CSS + """
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--v-bg); color: var(--v-ink);
  font-family: var(--v-font); font-size: 15px; line-height: 1.55; }
a { color: var(--v-violet-deep); text-underline-offset: 2px; }
a:hover { color: var(--v-violet); }
.v-main { max-width: 1120px; margin: 0 auto; padding: 8px 20px 48px; }
h1, h2, h3 { color: var(--v-ink); line-height: 1.25; letter-spacing: -.01em; }
.v-main h1 { font-size: 26px; margin: 20px 0 8px; }
h2 { font-size: 19px; margin: 28px 0 10px; }
h3 { font-size: 16px; margin: 18px 0 8px; }
p { margin: 8px 0; }
.muted { color: var(--v-muted); font-size: 14px; }
.unavailable { color: var(--v-muted); font-style: italic; }
code { background: var(--v-violet-soft); color: var(--v-violet-deep);
  padding: 1px 6px; border-radius: 6px; font-size: 13px; }
blockquote { border-left: 4px solid var(--v-amber); margin: 12px 0;
  padding: 10px 14px; background: var(--v-amber-soft);
  border-radius: 0 var(--v-radius-sm) var(--v-radius-sm) 0; }

/* cards */
.card { background: var(--v-card); border: 1px solid var(--v-line);
  border-radius: var(--v-radius); padding: 18px 20px; margin: 14px 0;
  box-shadow: var(--v-shadow); }
.card h2, .card h3 { margin-top: 0; }
.big { font-size: 30px; font-weight: 800; color: var(--v-violet-deep);
  font-variant-numeric: tabular-nums; }

/* tables: a white card with a quiet header row */
.table-wrap { overflow-x: auto; background: var(--v-card);
  border: 1px solid var(--v-line); border-radius: var(--v-radius);
  box-shadow: var(--v-shadow); margin: 8px 0 22px; }
.table-wrap table { margin: 0; border: 0; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 22px;
  background: var(--v-card); }
th, td { padding: 11px 14px; text-align: left; font-size: 14px;
  vertical-align: top; border: 0; border-bottom: 1px solid var(--v-line); }
th { background: #faf9fe; color: var(--v-muted); font-size: 12px;
  font-weight: 700; text-transform: uppercase; letter-spacing: .05em;
  white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }

/* buttons */
button, .btn { display: inline-flex; align-items: center;
  justify-content: center; gap: 6px; min-height: 44px; padding: 0 18px;
  margin: 4px 6px 4px 0; border-radius: var(--v-radius-sm);
  border: 1px solid var(--v-line-strong); background: var(--v-card);
  color: var(--v-ink); font: inherit; font-size: 15px; font-weight: 600;
  cursor: pointer; text-decoration: none; text-align: center;
  transition: background .15s, border-color .15s, box-shadow .15s; }
button:hover, .btn:hover { border-color: var(--v-violet-line);
  background: #fbfaff; color: var(--v-ink); }
button:focus-visible, .btn:focus-visible, a:focus-visible,
input:focus-visible, select:focus-visible {
  outline: 3px solid rgba(139, 92, 246, .35); outline-offset: 2px; }
.btn-primary, button.primary { background: var(--v-violet-deep);
  border-color: var(--v-violet-deep); color: #fff; }
.btn-primary:hover, button.primary:hover { background: #5b21b6;
  border-color: #5b21b6; color: #fff; }
.btn-google { background: #fff; border-color: var(--v-line-strong); }
button[name="approved"][value="true"] { background: var(--v-green);
  border-color: var(--v-green); color: #fff; }
button[name="approved"][value="false"] { background: var(--v-card);
  border-color: #fca5a5; color: var(--v-red); }

/* forms */
label { display: block; margin: 14px 0 6px; font-weight: 600;
  font-size: 14px; }
input[type=text], input[type=url], input[type=email],
input[type=password], input[type=number], select, textarea {
  width: 100%; min-height: 44px; padding: 9px 12px; font: inherit;
  font-size: 16px; color: var(--v-ink); background: #fff;
  border: 1px solid var(--v-line-strong); border-radius: var(--v-radius-sm); }
input:focus, select:focus, textarea:focus { outline: none;
  border-color: var(--v-violet); box-shadow: 0 0 0 3px rgba(139, 92, 246, .18); }

/* badges / pills */
.badge, .pill { display: inline-block; padding: 3px 10px;
  border-radius: 999px; font-size: 12px; font-weight: 700;
  background: var(--v-violet-soft); color: var(--v-violet-deep);
  border: 0; white-space: nowrap; }
.badge-off { background: #f1f1f5; color: var(--v-muted); }
.badge-ok, .pill.green { background: var(--v-green-soft); color: var(--v-green); }
.pos { color: var(--v-green); }
.neg { color: var(--v-red); }

/* notices */
.v-notice { border-radius: var(--v-radius-sm); padding: 12px 16px;
  margin: 12px 0; font-weight: 600; border: 1px solid; }
.v-notice-ok { background: var(--v-green-soft); border-color: #86efac;
  color: #14532d; }
.v-notice-bad { background: var(--v-red-soft); border-color: #fca5a5;
  color: #7f1d1d; }
.v-notice-warn { background: var(--v-amber-soft); border-color: #fcd34d;
  color: #78350f; }

/* the full proposed action, shown before deciding (Approvals rows
   and the Overview decision strip): the raw payload as a small
   scrollable block. */
.v-approval-payload { font-size: 13px; background: #faf9fe;
  border: 1px solid var(--v-line); border-radius: 8px; padding: 10px 12px;
  overflow-x: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
/* The expandable payload blocks (Approvals rows, Overview decision
   strip): the summary line is the click target. A class, not the
   bare element name — the bare name would trip the shell-404 test's
   bare-JSON check. */
.v-expandable > summary { cursor: pointer; font-weight: 600; }

/* simple stat row (operator tools) */
.v-kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px; margin: 14px 0 6px; }
.v-kpi { background: var(--v-card); border: 1px solid var(--v-line);
  border-radius: var(--v-radius); padding: 14px 16px; box-shadow: var(--v-shadow); }
.v-kpi .label { display: block; font-size: 12px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .05em; color: var(--v-muted); }
.v-kpi .value { display: block; font-size: 24px; font-weight: 800;
  margin-top: 4px; font-variant-numeric: tabular-nums; }

.v-back { display: inline-block; margin-top: 18px; font-weight: 600; }

@media (max-width: 560px) {
  .v-main { padding: 4px 16px 40px; }
  .v-main h1 { font-size: 22px; }
  th, td { font-size: 13px; padding: 9px 10px; }
  .btn { width: 100%; margin: 4px 0; }
}
"""


# Rendered markdown documents (mission reports): readable measure.
DOC_CSS = """
.v-doc { max-width: 820px; padding: 24px 28px; }
.v-doc h1 { margin-top: 0; font-size: 24px; }
.v-doc img { max-width: 100%; height: auto; }
.v-doc pre { overflow-x: auto; background: #faf9fe; padding: 12px;
  border-radius: var(--v-radius-sm); border: 1px solid var(--v-line); }
@media (max-width: 560px) { .v-doc { padding: 18px 16px; } }
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def money(value: float | None, places: int = 2) -> str:
    """$1,234.50 / −$0.80 — the sign goes before the dollar sign."""
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}${abs(value):,.{places}f}"


def pause_card_html(paused: bool, next_path: str) -> str:
    """The operator-global pause control: the only way to stop the
    agents working. Pausing stops the orchestrator tick loop (no
    ticks run while paused, including any automatic ticking);
    on-demand analyze jobs are untouched. The state is read from
    the store on every render, so the button always shows the
    truth. Shared by the Overview button and the Settings control
    (My account, operator only)."""
    if paused:
        state_html = ("<p><strong>Agents are paused.</strong> No "
                      "agent work runs while paused — ticks are "
                      "refused until you resume.</p>")
        btn = "Resume all agents"
        value = "0"
    else:
        state_html = ("<p><strong>Agents are running.</strong> "
                      "Pause stops the agents working — no ticks "
                      "run while paused.</p>")
        btn = "Pause all agents"
        value = "1"
    return (f"<div class='card'><h2>Agents</h2>{state_html}"
            f"<form method='post' action='/api/pause'>"
            f"<input type='hidden' name='paused' value='{value}'>"
            f"<input type='hidden' name='next' value='{esc(next_path)}'>"
            f"<p><button class='btn' type='submit'>{btn}</button></p>"
            f"</form></div>")


def pause_pill_html(paused: bool, next_path: str) -> str:
    """The slim pause pill for the Overview header row (operator
    only): the same POST /api/pause as the full card, compact.
    Running shows the pause icon + "Agents running — Pause";
    paused shows the play icon + "Agents paused — Resume". The
    label uses an em dash (never the middle dot — the vector-only
    test bans it). On narrow screens the text hides (CSS) and the
    icon alone carries the state."""
    from ventoryn.icons import icon

    if paused:
        label = "Agents paused — Resume"
        value = "0"
        mark = icon("play", 15)
    else:
        label = "Agents running — Pause"
        value = "1"
        mark = icon("pause", 15)
    return (f"<form class='pause-pill-form' method='post' action='/api/pause'>"
            f"<input type='hidden' name='paused' value='{value}'>"
            f"<input type='hidden' name='next' value='{esc(next_path)}'>"
            f"<button class='pause-pill' type='submit' "
            f"aria-label='{esc(label)}' title='{esc(label)}'>"
            f"{mark}<span>{esc(label)}</span></button></form>")


def page(title: str, inner: str, *, header: str = "",
         extra_css: str = "", body_class: str = "",
         head_extra: str = "", sidebar: str = "",
         page_head: str = "") -> str:
    """A complete HTML document in the Ventoryn shell.

    ``header`` is an app bar (legacy, unused now that every
    signed-in page — admin included — uses the platform shell) or
    empty for pages without navigation (unsigned error pages, the
    admin login).
    ``inner`` goes inside the centered <main>.

    Platform shell: pass ``sidebar`` (from ventoryn.nav.sidebar_html)
    and ``page_head`` (from ventoryn.nav.platform_head_html) and the
    page renders as ``<div class="app-layout">{sidebar}<div
    class="app-content">{page_head}<main class="app-main">{inner}</main>
    </div></div>`` with the platform CSS + JS. When ``sidebar`` is
    empty, the existing centered rendering is unchanged.

    Import-cycle note: the platform CSS/JS live in ventoryn.nav,
    which imports only ventoryn.icons — never this module — so the
    lazy import below cannot cycle.
    """
    cls = f" class='{esc(body_class)}'" if body_class else ""
    if sidebar:
        from ventoryn.nav import SIDEBAR_CSS, SIDEBAR_JS

        return (
            "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            f"<title>{esc(title)} — Ventoryn</title>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<meta name='theme-color' content='#0b0e1e'>"
            f"<style>{BASE_CSS}{SIDEBAR_CSS}{extra_css}</style>{head_extra}"
            f"</head><body{cls}><div class='app-layout'>{sidebar}"
            f"<div class='app-content'>{page_head}"
            f"<main class='app-main'>{inner}</main></div></div>"
            f"{SIDEBAR_JS}</body></html>")
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{esc(title)} — Ventoryn</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='theme-color' content='#0b0e1e'>"
        f"<style>{BASE_CSS}{extra_css}</style>{head_extra}"
        f"</head><body{cls}>{header}<main class='v-main'>{inner}</main>"
        "</body></html>")
