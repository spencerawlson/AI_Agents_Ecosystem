"""Publishers for SEO engine article packages.

Two real targets, both draft-first:

  - WordPressPublisher: POSTs to the site's WordPress REST API
    (/wp-json/wp/v2/posts) with an application password. Default status
    is "draft" — going live requires an explicit status="publish" from
    the caller, which the mission only passes behind its own flag.
  - FileExportPublisher: writes the rendered article (front matter +
    JSON-LD + Markdown) into a directory, for Spencer's Next.js / static
    sites (Farelune, ProofFlow, Domainfolio). The file lands as a draft
    artifact a human commits to the site repo — nothing goes live by
    itself. Refuses to overwrite an existing slug.

The Markdown -> HTML conversion covers the controlled subset the writer
produces (H2/H3, paragraphs, bold, links, bullet lists, FAQ blockquote
free). It is deliberately small and documented as such.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from core.seo_content import build_jsonld, render_markdown


class PublisherError(RuntimeError):
    """Raised when a publish attempt fails; nothing is half-published."""


def markdown_to_html(md: str) -> str:
    """Convert the writer's Markdown subset to HTML for WordPress."""
    out: list[str] = []
    in_list = False

    def inline(text: str) -> str:
        text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                      r'<a href="\2">\1</a>', text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
        return text

    for raw in md.splitlines():
        line = raw.strip()
        if line.startswith("- ") or line.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line:
            continue
        if line.startswith("### "):
            out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h2>{inline(line[2:])}</h2>")
        else:
            out.append(f"<p>{inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


class WordPressPublisher:
    """Real WordPress REST publisher (application-password auth)."""

    def __init__(self, site_url: str, username: str, app_password: str,
                 timeout: int = 30) -> None:
        if not (username and app_password):
            raise PublisherError(
                "WordPress publishing needs a username and application "
                "password (WP_USER / WP_APP_PASSWORD)")
        self.base = site_url.rstrip("/")
        self.timeout = timeout
        token = base64.b64encode(
            f"{username}:{app_password}".encode()).decode()
        self._auth = f"Basic {token}"

    def create_post(self, pkg: dict, status: str = "draft") -> dict:
        if status not in ("draft", "publish"):
            raise PublisherError(f"refusing unknown post status: {status!r}")
        jsonld = build_jsonld(pkg, self.base)
        content = markdown_to_html(str(pkg.get("body_markdown", "")))
        content += ('\n<script type="application/ld+json">'
                    + json.dumps(jsonld) + "</script>")
        payload = {
            "title": pkg.get("title", ""),
            "slug": pkg.get("slug", ""),
            "status": status,
            "content": content,
            "excerpt": pkg.get("meta_description", ""),
        }
        req = urllib.request.Request(
            f"{self.base}/wp-json/wp/v2/posts",
            data=json.dumps(payload).encode(),
            headers={"Authorization": self._auth,
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise PublisherError(
                f"WordPress rejected the post (HTTP {exc.code}): {detail}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise PublisherError(f"WordPress publish failed: {exc}") from exc
        return {"id": body.get("id"), "link": body.get("link"),
                "status": body.get("status"), "slug": body.get("slug")}


class FileExportPublisher:
    """Writes a draft article package into a directory for static sites."""

    def __init__(self, out_dir: str | Path) -> None:
        self.out_dir = Path(out_dir)

    def publish(self, pkg: dict, site_url: str, author: str = "") -> dict:
        slug = str(pkg.get("slug") or "")
        if not slug:
            raise PublisherError("package has no slug")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        md_path = self.out_dir / f"{slug}.md"
        schema_path = self.out_dir / f"{slug}.schema.json"
        if md_path.exists() or schema_path.exists():
            raise PublisherError(
                f"refusing to overwrite existing export for slug: {slug}")
        jsonld = build_jsonld(pkg, site_url, author=author)
        md_path.write_text(render_markdown(pkg, jsonld), encoding="utf-8")
        schema_path.write_text(json.dumps(jsonld, indent=2), encoding="utf-8")
        return {"slug": slug, "markdown": str(md_path),
                "schema": str(schema_path), "status": "exported_draft"}
