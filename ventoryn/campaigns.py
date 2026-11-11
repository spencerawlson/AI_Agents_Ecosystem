"""X campaigns: drafted by the agents, approved by the owner, posted on
a schedule.

Life of a campaign:

1. **Proposed** — the agent (job ``x_campaign``, started from Agents or
   by the weekly autopilot) drafts the posts from the website's own
   pages and their titles: one post per day slot, each linking to a
   real page of the site (signed when posted, so sales are counted).
   Nothing is posted. The proposal waits in Approvals with its cost
   estimate.
2. **Scheduled** — the owner approves it with a fresh 2FA code. Each
   remaining post gets a date (posts spread evenly from now). Posts can
   be removed one by one before approving.
3. **Posted** — the autopilot loop posts each draft when its date
   comes, through the same gate as a manual post (post_to_x: posting
   pause, X connection, monthly X budget). A post that cannot go out is
   *held* with the reason, never retried in a loop.

The agent never invents facts: it may only use the site's page titles
and URLs. Every post is checked: length, a link from the site only,
no duplicates.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

from ventoryn.campaign_cost import estimate_x_campaign, describe

MAX_POSTS = 60          # one proposal never asks for more approvals than this
X_MAX_CHARS = 280
X_LINK_CHARS = 24       # X counts any link as 23 characters, plus a space


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def plan_size(days: int, posts_per_day: float) -> int:
    return max(1, min(MAX_POSTS, int(round(float(posts_per_day) * int(days)))))


def validate_posts(raw: list, *, allowed_urls: set[str], fallback_url: str,
                   with_link: bool, want: int) -> list[dict]:
    """Keep only usable posts: text that fits X with its link, links
    only to the site's own pages (anything else becomes the home page),
    no duplicates, at most ``want``."""
    out: list[dict] = []
    seen: set[str] = set()
    limit = X_MAX_CHARS - (X_LINK_CHARS if with_link else 0)
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        text = _clean(item.get("text"))
        text = re.sub(r"https?://\S+", "", text).strip()  # the link is added by us
        if not text or len(text) > limit:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        link = ""
        if with_link:
            link = str(item.get("link") or "").strip()
            if link not in allowed_urls:
                link = fallback_url
        out.append({"text": text, "link": link})
        if len(out) >= want:
            break
    return out


def draft_posts(site: dict, *, days: int, posts_per_day: float,
                with_link: bool, llm=None, inventory=None) -> list[dict]:
    """Ask the AI for the campaign's posts. Raises RuntimeError when it
    cannot produce at least one usable post."""
    from agents.marketing.agent import clean_llm_text
    from core.llm import LLMGateway
    from core.seo_content import fetch_inventory

    want = plan_size(days, posts_per_day)
    if inventory is None:
        try:
            inventory = fetch_inventory(site["url"], max_pages=25)
        except ValueError:
            inventory = None
    pages = [(p.url, getattr(p, "title", "") or "") for p in
             (inventory.pages if inventory else [])][:25]
    allowed = {u for u, _ in pages} | {site["url"]}
    page_list = "\n".join(f"- {u}  ({t})" for u, t in pages) or f"- {site['url']}"
    limit = X_MAX_CHARS - (X_LINK_CHARS if with_link else 0)
    prompt = (
        f"You write X (Twitter) posts for the website {site['url']} "
        f"(market: {site.get('country', '')}, language: {site.get('language', 'en')}).\n"
        f"Write {want} DIFFERENT posts for a {days}-day campaign.\n\n"
        f"The ONLY facts you may use are these pages and their titles — never "
        f"invent prices, numbers, features, testimonials or claims:\n{page_list}\n\n"
        f"Rules: each post is at most {limit} characters, written in the site's "
        f"language, helpful and specific, no hashtag stuffing (0-2 hashtags), "
        f"no URLs in the text"
        + (", and pick for each post the most relevant page above as its link"
           if with_link else "") + ".\n"
        'Return ONLY JSON: {"posts": [{"text": "...", "link": "<one page URL above>"}]}')
    gateway = llm or LLMGateway()
    result = gateway.complete(prompt, tier=getattr(LLMGateway, "SMART", "smart"),
                              system="You are a careful social media writer. JSON only.",
                              json_mode=True, timeout=180)
    payload = clean_llm_text(result.get("json") or {})
    posts = validate_posts((payload or {}).get("posts") or [], allowed_urls=allowed,
                           fallback_url=site["url"], with_link=with_link, want=want)
    if not posts:
        raise RuntimeError("the agent returned no usable posts")
    return posts


def propose(store, site: dict, posts: list[dict], *, days: int,
            posts_per_day: float, with_link: bool, source: str) -> dict:
    """Save a proposal: the campaign plus its pending drafts."""
    campaign = store.create_campaign(site["id"], site["account_id"], days=days,
                                     posts_per_day=posts_per_day,
                                     with_link=with_link, source=source)
    for p in posts:
        store.create_social_draft(site["id"], "x", p["text"], p["link"],
                                  campaign_id=campaign["id"])
    return campaign


def schedule(store, campaign: dict, now: datetime | None = None) -> int:
    """Approve: give each remaining draft a posting date, spread evenly
    over the campaign from now. Returns how many were scheduled."""
    now = now or datetime.now(timezone.utc)
    drafts = [d for d in store.campaign_drafts(campaign["id"]) if d["status"] == "pending"]
    if not drafts:
        return 0
    step = timedelta(days=int(campaign["days"])) / len(drafts)
    for i, d in enumerate(drafts):
        store.schedule_draft(d["id"], (now + step * i).isoformat())
    store.set_campaign_status(campaign["id"], "scheduled")
    return len(drafts)


def reject(store, campaign: dict) -> None:
    for d in store.campaign_drafts(campaign["id"]):
        store.remove_campaign_draft(d["id"])
    store.set_campaign_status(campaign["id"], "rejected")


def post_due(store, config, now: datetime | None = None, *, poster=None) -> list[tuple]:
    """Post every scheduled draft whose date has come. A draft that
    cannot be posted is held with the reason (no retry loop)."""
    from ventoryn.saas import post_to_x

    poster = poster or post_to_x
    now = now or datetime.now(timezone.utc)
    done = []
    for d in store.due_scheduled_drafts(now.isoformat()):
        site = store.get_site(d["site_id"])
        account = store.get_account(site["account_id"]) if site else None
        if account is None:
            store.hold_draft(d["id"], "Website no longer exists.")
            continue
        ok, message = poster(store, config, account, d["site_id"], d["id"])
        if not ok:
            store.hold_draft(d["id"], message)
        done.append((d["id"], ok, message))
    return done


# -- Approvals: the proposal card ---------------------------------------------

def _esc(v) -> str:
    return html.escape(str(v), quote=True)


def proposal_card(store, config, campaign: dict, totp_field: str) -> str:
    """One proposed campaign, post by post, with its cost and the
    Approve (2FA) / Reject buttons."""
    site = store.get_site(campaign["site_id"]) or {}
    drafts = [d for d in store.campaign_drafts(campaign["id"]) if d["status"] == "pending"]
    est = estimate_x_campaign(posts_per_day=len(drafts) / max(1, campaign["days"]),
                              days=campaign["days"], with_link=bool(campaign["with_link"]),
                              spent_this_month_usd=store.x_spend_this_month_usd(site.get("id", "")),
                              monthly_budget_usd=config.x_monthly_budget_usd)
    est["posts"] = len(drafts)
    est["total_usd"] = round(len(drafts) * est["rate_usd"], 2)
    x_conn = (store.get_connection(site.get("id", ""), "x") or {}).get("status") == "connected"
    connect = ("" if x_conn else
               f"<p class='v-camp-warn'>X is not connected for this website — approved posts "
               f"will wait until you <a href='/integrations/x?site={_esc(site.get('id', ''))}'>"
               f"connect X</a>.</p>")
    cid = _esc(campaign["id"])
    rows = "".join(
        f"<li><p>{_esc(d['text'])}</p>"
        + (f"<p class='muted'>Link: {_esc(d['link_url'])}</p>" if d["link_url"] else "")
        + f"<form method='post' action='/campaigns/{cid}/posts/{_esc(d['id'])}/remove'>"
          f"<button class='btn btn-small' type='submit'>Remove</button></form></li>"
        for d in drafts)
    who = "Proposed by the autopilot" if campaign["source"] == "autopilot" else "Started by you"
    return (
        f"<div class='card v-camp' id='campaign-{cid}'>"
        f"<h3>X campaign — {_esc(site.get('host') or site.get('url', ''))}</h3>"
        f"<p class='muted'>{who} on {_esc(str(campaign['created_at'])[:10])} — "
        f"{campaign['days']} days, {len(drafts)} posts.</p>"
        f"<p class='v-camp-cost'><strong>Cost:</strong> {_esc(describe(est))}</p>{connect}"
        f"<details><summary>Read the {len(drafts)} posts</summary><ol class='v-camp-posts'>{rows}</ol></details>"
        f"<form method='post' action='/campaigns/{cid}/approve'>{totp_field}"
        f"<p><button class='btn btn-primary' type='submit'>Approve campaign</button> "
        f"<button class='btn' type='submit' formaction='/campaigns/{cid}/reject'>Reject</button></p>"
        f"<p class='muted'>Approving schedules the posts over {campaign['days']} days; each is "
        f"posted on its date within this website's monthly X budget.</p></form></div>")


def scheduled_summary(store, campaign: dict) -> str:
    drafts = store.campaign_drafts(campaign["id"])
    posted = sum(1 for d in drafts if d["status"] == "posted")
    waiting = sum(1 for d in drafts if d["status"] == "scheduled")
    held = [d for d in drafts if d["status"] == "held"]
    site = store.get_site(campaign["site_id"]) or {}
    note = (f" — {len(held)} held ({_esc(held[0]['note'])})" if held else "")
    return (f"<li><strong>{_esc(site.get('host', ''))}</strong>: {posted} posted, "
            f"{waiting} scheduled{note}</li>")


CAMPAIGN_CSS = """
.v-camp-posts { padding-left: 20px; }
.v-camp-posts li { margin: 0 0 12px; }
.v-camp-posts li p { margin: 0 0 4px; }
.v-camp-posts form { margin: 0; }
.v-camp-warn { color: #92400e; }
.btn-small { padding: 4px 10px; font-size: 12px; min-height: 0; }
"""
