"""Mission reports for the dashboard GUI.

Reports are Markdown files written by mission scripts (e.g.
ecosystem/marketing_mission.py) into <repo>/reports/. This module lists
them newest-first and renders a safe HTML subset — exactly what the
missions emit: #/##/### headings, > blockquotes, **bold**, _italic_,
`code`, | tables |, - lists, --- rules. Everything else is escaped,
so a report can never inject markup or scripts.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path

_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*\.md")
_TITLE_RE = re.compile(r"^#\s+(.*)")
_GENERATED_RE = re.compile(r"_Generated\s+(\S+)")
_SPEND_RE = re.compile(r"Total AI spend this mission:\s+\*\*\$([\d,]+\.\d+)\*\*")
_DATE_RE = re.compile(r"_(\d{4})(\d{2})(\d{2})\.md$")


def reports_dir() -> Path:
    """Where mission scripts write their Markdown reports."""
    env = os.environ.get("REPORTS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "reports"


def _mission_label(name: str) -> str:
    base = _DATE_RE.sub("", name)
    if base.endswith(".md"):
        base = base[:-3]
    return base.replace("_", " ").strip().title() or "Mission"


def _date_label(name: str) -> str:
    m = _DATE_RE.search(name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def list_reports() -> list[dict]:
    """Report metadata, newest first. Never raises on a bad file."""
    directory = reports_dir()
    if not directory.is_dir():
        return []
    out = []
    for path in directory.glob("*.md"):
        if not _NAME_RE.fullmatch(path.name):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = _mission_label(path.name)
        generated = ""
        spend = None
        for line in text.split("\n"):
            if title == _mission_label(path.name):
                m = _TITLE_RE.match(line.strip())
                if m:
                    title = m.group(1).strip()
            if not generated:
                m = _GENERATED_RE.search(line)
                if m:
                    generated = m.group(1)
            if spend is None:
                m = _SPEND_RE.search(line)
                if m:
                    try:
                        spend = float(m.group(1).replace(",", ""))
                    except ValueError:
                        spend = None
            if title != _mission_label(path.name) and generated and spend is not None:
                break
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append({
            "name": path.name,
            "title": title,
            "mission": _mission_label(path.name),
            "generated_at": generated,
            "date": _date_label(path.name),
            "spend_usd": spend,
            "mtime": mtime,
        })
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def _inline(text: str) -> str:
    """Inline formatting on already-escaped text."""
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<em>\1</em>", text)
    return text


def _is_block_start(line: str) -> bool:
    s = line.strip()
    return (
        bool(re.match(r"^#{1,3}\s+", s))
        or bool(re.match(r"^---+\s*$", s))
        or s.startswith(">")
        or s.startswith("|")
        or s.startswith("- ")
    )


def _render_table(rows: list[list[str]]) -> str:
    header, body = rows[0], rows[1:]
    if len(rows) > 1 and all(re.fullmatch(r":?-+:?", c or "-") for c in rows[1]):
        body = rows[2:]
    thead = "<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in header) + "</tr>"
    tbody = "".join(
        "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
        for row in body
    )
    return f"<table>{thead}{tbody}</table>"


def render_markdown(text: str) -> str:
    """Render the mission-report Markdown subset to safe HTML."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", s)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if re.match(r"^---+\s*$", s):
            out.append("<hr>")
            i += 1
            continue
        if s.startswith(">"):
            quoted = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quoted.append(lines[i].strip()[1:].lstrip())
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(quoted))}</blockquote>")
            continue
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in
                             lines[i].strip().strip("|").split("|")])
                i += 1
            out.append(_render_table(rows))
            continue
        if s.startswith("- "):
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(_inline(lines[i].strip()[2:]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
            continue
        paras = []
        while i < len(lines) and lines[i].strip() and not _is_block_start(lines[i]):
            paras.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(paras))}</p>")
    return "\n".join(out)


def render_report(name: str) -> tuple[str, str] | None:
    """Return (title, html) for a report, or None if unknown/invalid.

    The name is strictly validated and resolved inside the reports
    directory, so path traversal is impossible.
    """
    if not _NAME_RE.fullmatch(name):
        return None
    path = (reports_dir() / name).resolve()
    try:
        if path.parent != reports_dir().resolve() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    title = _mission_label(name)
    m = _TITLE_RE.match(text.lstrip().split("\n", 1)[0].strip())
    if m:
        title = m.group(1).strip()
    return title, render_markdown(text)
