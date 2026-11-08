"""Creative Agent: generates and versions creative assets.

Asset types: product images, ad concepts, social content, copy,
email campaigns. Every asset is versioned and linked to the
campaign it serves, so performance can be attributed back to
the creative that drove it.
"""

from __future__ import annotations

import threading
from datetime import datetime

from pydantic import BaseModel, Field

from agents.base import BaseAgent
from orchestrator.models import Task, utcnow


class CreativeAsset(BaseModel):
    id: str
    business_id: str
    campaign_id: str | None = None
    asset_type: str  # image | ad_concept | social_post | copy | email | video_concept | video-script | community
    title: str
    content: str  # copy text, image prompt, or asset reference
    version: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    # Performance attribution (filled by marketing agent feedback).
    impressions: int = 0
    clicks: int = 0
    conversions: int = 0


class CreativeAgent(BaseAgent):
    agent_type = "creative"
    capabilities = [
        "copywriting",
        "ad_concepts",
        "social_content",
        "email_campaigns",
        "seo_articles",
    ]
    description = "Generates versioned creative assets tied to campaigns."

    def __init__(self, signer=None) -> None:
        super().__init__()
        self._assets: dict[str, CreativeAsset] = {}
        self._counter = 0
        # The dispatcher may run concurrent tasks on this one handler
        # instance: ID generation must never hand out a duplicate.
        self._id_lock = threading.Lock()
        # Ventoryn signature (optional): a callable
        # ``(url, agent) -> (signed_url, tag)`` supplied by the SaaS
        # shell when the agent works for a Ventoryn site (see
        # ventoryn.signature.sign_agent_link). Reply drafts are signed
        # through it; without it drafts stay unsigned and the output
        # says so.
        self._signer = signer

    def _next_id(self) -> str:
        with self._id_lock:
            self._counter += 1
            return f"asset_{self._counter:05d}"

    def create(
        self,
        business_id: str,
        asset_type: str,
        title: str,
        content: str,
        campaign_id: str | None = None,
    ) -> CreativeAsset:
        asset = CreativeAsset(
            id=self._next_id(),
            business_id=business_id,
            campaign_id=campaign_id,
            asset_type=asset_type,
            title=title,
            content=content,
        )
        self._assets[asset.id] = asset
        return asset

    def new_version(self, asset_id: str, content: str) -> CreativeAsset:
        """Create a new version of an existing asset (immutable history)."""
        old = self._assets.get(asset_id)
        if old is None:
            raise ValueError(f"unknown asset: {asset_id}")
        new = CreativeAsset(
            id=self._next_id(),
            business_id=old.business_id,
            campaign_id=old.campaign_id,
            asset_type=old.asset_type,
            title=old.title,
            content=content,
            version=old.version + 1,
        )
        self._assets[new.id] = new
        return new

    def record_performance(
        self, asset_id: str, impressions: int, clicks: int, conversions: int
    ) -> CreativeAsset:
        asset = self._assets.get(asset_id)
        if asset is None:
            raise ValueError(f"unknown asset: {asset_id}")
        # Read-modify-write on shared state: guard it for concurrent tasks.
        with self._id_lock:
            asset.impressions += impressions
            asset.clicks += clicks
            asset.conversions += conversions
        return asset

    def top_performers(self, business_id: str, limit: int = 5) -> list[CreativeAsset]:
        assets = [a for a in self._assets.values() if a.business_id == business_id]
        return sorted(assets, key=lambda a: a.conversions, reverse=True)[:limit]

    def run(self, task: Task) -> dict:
        action = task.inputs.get("action", "report")
        if action == "report":
            self.record_usage(task, tokens=200, cost_usd=0.01)
            return {
                "total_assets": len(self._assets),
                "by_type": {
                    t: sum(1 for a in self._assets.values() if a.asset_type == t)
                    for t in {a.asset_type for a in self._assets.values()}
                },
            }
        if action == "brief":
            title = task.inputs.get("title", "untitled")
            description = task.inputs.get("description", "")
            business_id = task.business_id or "unknown"
            asset = self.create(business_id, "copy", title, description)
            self.record_usage(task, tokens=800, cost_usd=0.02)
            return {
                "asset_id": asset.id,
                "title": title,
                "status": "briefed",
                "next": "human reviews brief, produces final product",
            }
        if action == "social_drafts":
            from core.llm import LLMGateway

            if not LLMGateway.enabled():
                raise RuntimeError(
                    "social_drafts requires LLM mode (--llm with OPENAI_API_KEY)"
                )
            niche = task.inputs.get("niche", "")
            themes = [t for t in task.inputs.get("themes", []) if t]
            count = int(task.inputs.get("count", 10))
            business_id = task.business_id or "unknown"
            theme_list = "\n".join(f"- {t}" for t in themes) or f"- {niche}"
            prompt = (
                f"You write social posts for an online learning platform in the "
                f"niche: {niche}.\n"
                f"Themes to cover:\n{theme_list}\n"
                f"Return ONLY a JSON object with a single key \"drafts\": a list of "
                f"exactly {count} drafts, each with keys \"platform\" (one of: x, "
                'threads, linkedin), "text" (the post, under 280 chars for x/threads), '
                '"hook" (the opening line, one sentence). No hashtags spam: max 3.'
            )
            result = LLMGateway().complete(
                prompt,
                tier="cheap",
                system="You are a concise social media copywriter. JSON only.",
                json_mode=True,
            )
            payload = result["json"] or {}
            drafts = payload.get("drafts", payload) if isinstance(payload, dict) else payload
            if not isinstance(drafts, list):
                raise ValueError("LLM did not return a drafts list")
            asset_ids: list[str] = []
            kept: list[dict] = []
            for d in drafts[:count]:
                if not isinstance(d, dict) or not d.get("text"):
                    continue
                platform = d.get("platform", "x")
                hook = d.get("hook", d["text"][:60])
                asset = self.create(
                    business_id,
                    "social",
                    f"[{platform}] {hook}"[:80],
                    f"[{platform}] {d['text']}",
                )
                asset_ids.append(asset.id)
                kept.append(
                    {"platform": platform, "text": d["text"], "hook": hook,
                     "asset_id": asset.id}
                )
            self.record_usage(
                task,
                tokens=result["input_tokens"] + result["output_tokens"],
                cost_usd=result["cost_usd"],
            )
            return {
                "niche": niche,
                "drafts": kept,
                "asset_ids": asset_ids,
                "evidence": (
                    f"{len(kept)} drafts returned; each draft has platform, "
                    "text, hook and is stored as a creative asset"
                ),
            }
        if action == "content_pieces":
            return self._content_pieces(task)
        if action == "community_replies":
            return self._community_replies(task)
        if action == "seo_article":
            return self._seo_article(task)
        raise ValueError(f"unknown action: {action}")

    def self_check(self, task: Task, output: dict) -> list[str]:
        if task.inputs.get("action") == "seo_article":
            return list((output.get("quality") or {}).get("errors", []))
        return []

    def _seo_article(self, task: Task) -> dict:
        """Write one SEO article package for the SEO engine mission.

        The contract is enforced on the way out by the deterministic
        gate in core.seo_content: the writer may only link to pages in
        the provided candidate set (drawn from the live site inventory),
        must hit the word band, and must ship a real FAQ. Findings ride
        along in the output ("quality") so the orchestrator's review
        layers and the mission report see exactly what the gate saw.
        Draft only — publishing is the mission's separate, gated step."""
        from agents.marketing.agent import clean_llm_text
        from core.llm import LLMGateway
        from core.seo_content import SiteInventory, SitePage, \
            validate_article_package

        if not LLMGateway.enabled():
            raise RuntimeError(
                "seo_article requires LLM mode (--llm with an API key)")
        keyword = str(task.inputs.get("keyword", "")).strip()
        if not keyword:
            raise ValueError("seo_article needs a target keyword")
        niche = task.inputs.get("niche", "")
        site_url = str(task.inputs.get("site_url", "")).rstrip("/")
        facts = [str(f) for f in task.inputs.get("facts", []) if f]
        candidates = [c for c in task.inputs.get("link_candidates", [])
                      if isinstance(c, dict) and c.get("url")]
        inventory_urls = [str(u) for u in
                          task.inputs.get("inventory_urls", []) if u]
        for c in candidates:
            if c["url"] not in inventory_urls:
                inventory_urls.append(c["url"])
        word_target = int(task.inputs.get("word_target", 1500))
        link_list = "\n".join(
            f"- {c['url']}  ({c.get('title', '')})" for c in candidates
        ) or "(none provided)"
        fact_list = "\n".join(f"- {f}" for f in facts) or "(no facts provided)"
        prompt = (
            f"You are an expert SEO writer for a site in this niche: {niche}.\n"
            f"Write ONE article targeting the keyword: \"{keyword}\".\n\n"
            f"SITE FACTS (the only facts you may assert — never invent "
            f"features, prices, statistics or testimonials):\n{fact_list}\n\n"
            f"ALLOWED INTERNAL LINKS (use at least 3, exactly as written, "
            f"woven into the body where they genuinely help the reader):\n"
            f"{link_list}\n\n"
            f"Return ONLY a JSON object with keys:\n"
            f"- \"title\": <= 60 characters, contains the keyword naturally\n"
            f"- \"meta_description\": 120-160 characters, written to earn "
            f"the click\n"
            f"- \"slug\": kebab-case, derived from the keyword\n"
            f"- \"target_keyword\": \"{keyword}\"\n"
            f"- \"body_markdown\": {word_target} words (±20%), Markdown with "
            f"## sections, a short intro, practical depth, and a closing "
            f"section that points to the site's relevant pages\n"
            f"- \"faq\": at least 3 entries {{\"q\": ..., \"a\": ...}} "
            f"answering real questions a searcher would ask\n"
            f"- \"internal_links\": the allowed URLs you actually used\n"
            f"Write like a knowledgeable human practitioner, not a content "
            f"farm: specific, direct, no filler, no keyword stuffing."
        )
        rework = task.inputs.get("_rework_feedback")
        if rework:
            prompt += f"\n\nYour previous attempt was rejected by review: {rework}"
        result = LLMGateway().complete(
            prompt, tier=LLMGateway.SMART,
            system="You are a precise, expert long-form SEO writer. JSON only.",
            json_mode=True, timeout=600)
        self.record_usage(
            task, tokens=result["input_tokens"] + result["output_tokens"],
            cost_usd=result["cost_usd"])
        payload = clean_llm_text(result["json"] or {})
        pkg = payload.get("package", payload) if isinstance(payload, dict) \
            else {}
        if not (isinstance(pkg, dict) and pkg.get("title")
                and pkg.get("body_markdown")):
            raise ValueError("LLM returned no usable article package")
        pkg.setdefault("target_keyword", keyword)
        inventory = SiteInventory(
            site_url=site_url,
            pages=[SitePage(url=u) for u in inventory_urls])
        quality = validate_article_package(
            pkg, inventory, min_words=int(word_target * 0.7))
        business_id = task.business_id or "unknown"
        asset = self.create(business_id, "seo_article",
                            str(pkg["title"])[:80],
                            str(pkg["body_markdown"]))
        return {
            "keyword": keyword,
            "package": pkg,
            "quality": quality,
            "asset_id": asset.id,
            "evidence": (
                f"article package for {keyword!r}: "
                f"{len(quality['errors'])} gate errors, "
                f"{len(quality['warnings'])} warnings; stored as creative "
                f"asset {asset.id}; nothing published"),
        }

    def _community_replies(self, task: Task) -> dict:
        """Draft replies to real threads the listener found. Drafts only —
        each valid reply is stored as a creative asset; nothing is
        posted. Every draft must pass the deterministic 90/10 gate in
        core.community_listening (help first, site mentioned once at
        the end, disclosed as the author's own); failures are dropped
        and reported, never posted."""
        from agents.marketing.agent import _compact, clean_llm_text
        from core.community_listening import validate_reply_draft
        from core.llm import LLMGateway

        if not LLMGateway.enabled():
            raise RuntimeError(
                "community_replies requires LLM mode (--llm with an API key)")
        threads = [t for t in task.inputs.get("threads", [])
                   if isinstance(t, dict) and t.get("id") and t.get("text") is not None]
        if not threads:
            raise ValueError("community_replies needs at least one thread")
        profile = task.inputs.get("profile", {})
        site_url = str(task.inputs.get("site_url", "")).rstrip("/")
        facts = [str(f) for f in task.inputs.get("facts", []) if f]
        fact_list = "\n".join(f"- {f}" for f in facts) or "(no facts provided)"
        thread_list = "\n\n".join(
            f"{n}. [{t.get('venue', '')}] {t.get('title', '')}\n"
            f"   intent {t.get('intent_score', '')}/100 — "
            f"{'; '.join(t.get('reasons', []))}\n"
            f"   {str(t.get('text', ''))[:800]}"
            for n, t in enumerate(threads, 1))
        prompt = (
            "You answer real questions in online communities for this "
            "website. Answer each thread below with a genuinely helpful "
            "reply.\n\n"
            f"SITE PROFILE (facts — never invent features, prices or stats):\n"
            f"{_compact(profile, 2000)}\n\n"
            f"SITE FACTS:\n{fact_list}\n\n"
            f"THREADS TO ANSWER:\n{thread_list}\n\n"
            "Rules, all hard: answer the actual question first, in your "
            "own expert voice — at least 200 characters of real help "
            "before any mention of the site. Mention the site AT MOST "
            "once, only in the final sentence, only if it genuinely "
            "helps this person, and disclose it as something you built "
            "(\"I built\"). At most one link (the site URL). No hype, no "
            "fake testimonials, no promo language (no discount, buy now, "
            "guaranteed, 100% pass). Each reply 400–1200 characters.\n\n"
            "Return ONLY a JSON object {\"replies\": [...]} with one entry "
            "per thread, in the same order: {\"thread_id\", \"text\"}."
        )
        rework = task.inputs.get("_rework_feedback")
        if rework:
            prompt += f"\n\nYour previous attempt was rejected by review: {rework}"
        result = LLMGateway().complete(
            prompt, tier="cheap",
            system="You are a precise, helpful community member. JSON only.",
            json_mode=True)
        self.record_usage(task, tokens=result["input_tokens"] + result["output_tokens"],
                          cost_usd=result["cost_usd"])
        payload = clean_llm_text(result["json"] or {})
        raw = payload.get("replies", []) if isinstance(payload, dict) else payload
        by_id = {t["id"]: t for t in threads}
        business_id = task.business_id or "unknown"
        replies, dropped = [], []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            thread = by_id.get(str(entry.get("thread_id", "")))
            if thread is None or not entry.get("text"):
                continue
            errors = validate_reply_draft(str(entry["text"]), site_url)
            if errors:
                dropped.append({"thread_id": thread["id"],
                                "errors": errors})
                continue
            # The Ventoryn signature: the site link inside the draft
            # is signed (UTM + vtn_ tag, recorded) when the shell
            # supplied a signer for this site. Without one the draft
            # is unsigned and the output says so — never faked.
            from core.community_listening import sign_reply_draft
            signed_text, tag = sign_reply_draft(
                str(entry["text"]), site_url, signer=self._signer)
            asset = self.create(
                business_id, "community",
                f"[{thread.get('venue', '')}] {thread.get('title', '')}"[:80],
                signed_text)
            replies.append({"thread_id": thread["id"],
                            "venue": thread.get("venue", ""),
                            "url": thread.get("url", ""),
                            "title": thread.get("title", ""),
                            "text": signed_text,
                            "signature_tag": tag,
                            "asset_id": asset.id})
        if not replies:
            raise ValueError("LLM returned no usable reply drafts")
        signed = sum(1 for r in replies if r.get("signature_tag"))
        out: dict = {
            "replies": replies,
            "evidence": (f"{len(replies)} reply drafts stored as creative "
                         f"assets; {len(dropped)} dropped by the 90/10 gate; "
                         f"{signed} signed with the Ventoryn signature; "
                         f"nothing posted"),
        }
        # An empty top-level field fails the orchestrator's peer review
        # ("empty or placeholder"), so "dropped" is only present when
        # something was actually dropped.
        if dropped:
            out["dropped"] = dropped
        return out

    # What each content type must contain, for the prompt and validation.
    CONTENT_SPECS = {
        "blog": ('{"type": "blog", "title", "target_keyword", "meta_description", '
                 '"body_markdown" (900-1400 words, H2 sections, ends with a CTA '
                 'to the site)}', ("title", "body_markdown")),
        "social": ('{"type": "social", "platform" (linkedin, x or threads), "text"}',
                   ("platform", "text")),
        "community": ('{"type": "community", "venue" (e.g. r/cissp), "context" (the '
                      'kind of question it answers), "text" (a genuinely helpful '
                      'answer; mention the site only at the end, disclosed as '
                      '"I built")}', ("venue", "text")),
        "email": ('{"type": "email", "subject", "body"}', ("subject", "body")),
        "landing": ('{"type": "landing", "route" (suggested URL path), "title", '
                    '"meta_description", "h1", "body_markdown"}',
                    ("route", "title", "body_markdown")),
        "video-script": ('{"type": "video-script", "title", "scenes": '
                         '[{"on_screen_text" (<=140 chars), "narration", '
                         '"duration_s" (3-15)}] (3-8 scenes, 15-90s total)}',
                         ("title", "scenes")),
    }

    def _content_pieces(self, task: Task) -> dict:
        """Draft the content one marketing experiment needs. Drafts only —
        each piece is stored as a creative asset; nothing is published."""
        from agents.marketing.agent import _compact, clean_llm_text
        from core.llm import LLMGateway

        if not LLMGateway.enabled():
            raise RuntimeError(
                "content_pieces requires LLM mode (--llm with an API key)")
        exp = task.inputs.get("experiment", {})
        profile = task.inputs.get("profile", {})
        types = [t for t in exp.get("content_needed", []) if t in self.CONTENT_SPECS]
        if not types:
            types = ["social"]
        per_type = {"blog": 1, "landing": 1, "email": 2, "social": 4,
                    "community": 3, "video-script": 1}
        wanted = "\n".join(
            f"- {per_type[t]} x {self.CONTENT_SPECS[t][0]}" for t in types)
        prompt = (
            "You produce marketing content for this website.\n"
            f"SITE PROFILE (facts — never invent features, prices or stats):\n"
            f"{_compact(profile, 3000)}\n\n"
            f"EXPERIMENT THIS CONTENT SERVES:\n{_compact(exp, 2000)}\n\n"
            "Write in a direct, practical, expert voice for IT and security "
            "learners. No hype, no fake testimonials, max 3 hashtags.\n"
            'Return ONLY a JSON object {"pieces": [...]} containing:\n' + wanted
        )
        rework = task.inputs.get("_rework_feedback")
        if rework:
            prompt += f"\n\nYour previous attempt was rejected by review: {rework}"
        result = LLMGateway().complete(
            prompt, tier="cheap",
            system="You are a precise technical content marketer. JSON only.",
            json_mode=True)
        self.record_usage(task, tokens=result["input_tokens"] + result["output_tokens"],
                          cost_usd=result["cost_usd"])
        payload = clean_llm_text(result["json"] or {})
        raw = payload.get("pieces", []) if isinstance(payload, dict) else payload
        business_id = task.business_id or "unknown"
        pieces = []
        for p in raw if isinstance(raw, list) else []:
            if not isinstance(p, dict) or p.get("type") not in types:
                continue
            if not all(p.get(k) for k in self.CONTENT_SPECS[p["type"]][1]):
                continue
            label = p.get("title") or p.get("subject") or p.get("venue") \
                or p.get("platform") or p["type"]
            body = (p.get("body_markdown") or p.get("text")
                    or p.get("body") or "")
            if not body and p.get("scenes"):
                # Structured pieces (video-script) carry their content
                # as data, not prose — store the JSON so the renderer
                # can read the asset back.
                import json

                body = json.dumps(p, indent=2)
            asset = self.create(business_id, p["type"], f"[{exp.get('slug', '')}] "
                                f"{label}"[:80], body)
            pieces.append({**p, "asset_id": asset.id})
        if not pieces:
            raise ValueError("LLM returned no usable content pieces")
        return {
            "experiment": exp.get("slug", ""),
            "pieces": pieces,
            "evidence": f"{len(pieces)} content drafts ({', '.join(sorted({p['type'] for p in pieces}))}) "
                        "stored as creative assets; nothing published",
        }
