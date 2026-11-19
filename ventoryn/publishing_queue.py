"""The publishing queue (Gap 6): approved drafts waiting for their
channel, with an honest per-channel state.

Per-channel states, derived from the actual connection/config state
in the SaasStore + SaasConfig — never stored, never hardcoded in a
way that goes stale. A channel's state flips automatically when its
connection or its config changes:

* ``ready`` — the channel is connected and every operator/provider
  gate for it is cleared. A human-approved draft may go out.
* ``blocked`` — the channel is known but unusable right now, with
  the exact reason (e.g. "Meta business verification step 3
  pending").
* ``not_configured`` — no connection for this channel (nothing to
  connect with yet, or the site never connected it).

Derivation order, uniform for every channel:

1. Operator app/config missing -> ``not_configured``.
2. Known provider-side gate not cleared -> ``blocked``.
3. Site connection not ``connected`` -> ``not_configured``.
4. No publish path built in Axionory yet -> ``blocked``.
5. Otherwise -> ``ready``.

The gate facts come from the marketing API registry (verified
2026-11-19, ``~/workspace/marketing-api-registry/``): Meta business
verification step 3 pending; TikTok Content Posting app in review
since Nov 12, 2026, plus a separate Content Posting audit for public
posts; X posts fail HTTP 402 on a zero prepaid balance; LinkedIn /
YouTube / Pinterest / Reddit have no developer app or account. The
*reasons* and *unblock steps* are that registry's words; the *state*
is always re-derived from live store/config state.

Rules this module follows (do not weaken):

* Nothing in the queue publishes by itself. Publishing happens only
  through the existing per-channel post paths after a human approval,
  and only via ``request_publish`` with an injected publish function.
* A ``blocked`` / ``not_configured`` channel refuses a publish
  attempt with a clear reason — the publish function is never
  called, the item stays queued, nothing is silently dropped and
  nothing fake-succeeds.
* A channel that has never posted successfully is never shown as
  working: ``ever_posted`` is derived from the store (a ``posted``
  social draft, or a TikTok post that reached PUBLISH_COMPLETE),
  never inferred from a connection.
* No live calls at import time; tests use fakes only. TikTok's
  operator-app state is read from the environment through
  ``ventoryn.tiktok`` (``TIKTOK_CLIENT_KEY`` / ``TIKTOK_CLIENT_SECRET``
  / ``TIKTOK_AUDITED``) — the same env ``tiktok.py`` already reads.
"""

from __future__ import annotations

from ventoryn import tiktok as _tiktok
from ventoryn.saas import SaasConfig, SaasStore

READY = "ready"
BLOCKED = "blocked"
NOT_CONFIGURED = "not_configured"

# Channels in display order. ``kind`` is the SaasStore connection
# kind, or None when the channel has no connection kind at all yet
# (LinkedIn / YouTube / Pinterest). ``publish_path`` names the
# existing send path, or None when Axionory has not built one.
CHANNELS: tuple[str, ...] = (
    "facebook",
    "instagram",
    "x",
    "tiktok",
    "reddit",
    "linkedin",
    "youtube",
    "pinterest",
)

_DISPLAY_NAMES: dict[str, str] = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "x": "X",
    "tiktok": "TikTok",
    "reddit": "Reddit",
    "linkedin": "LinkedIn",
    "youtube": "YouTube",
    "pinterest": "Pinterest",
}

_STORE_KINDS: dict[str, str | None] = {
    "facebook": "facebook",
    "instagram": "instagram",
    "x": "x",
    "tiktok": "tiktok",
    "reddit": "reddit",
    "linkedin": None,
    "youtube": None,
    "pinterest": None,
}

#: Which existing send path a channel uses, when one exists.
#: "draft-approve" is the site-page Approve & post route;
#: "tiktok-posting-screen" is TikTok's own posting screen
#: (``ventoryn.tiktok``) — TikTok has no draft-approve route.
_PUBLISH_PATHS: dict[str, str | None] = {
    "facebook": "draft-approve",
    "instagram": "draft-approve",
    "x": "draft-approve",
    "tiktok": "tiktok-posting-screen",
    "reddit": None,
    "linkedin": None,
    "youtube": None,
    "pinterest": None,
}

#: The exact step that would unblock each channel, from the registry
#: (verified 2026-11-19). These are steps, not states — the state is
#: derived; the step only names what has to happen next.
_UNBLOCK_STEPS: dict[str, str] = {
    "facebook": (
        "Complete Meta business verification (legal details + document "
        "+ 2FA + an @axionory.com contact email), then App Review for "
        "ads_read/ads_management and Full-access qualification."),
    "instagram": (
        "Complete Meta business verification (legal details + document "
        "+ 2FA + an @axionory.com contact email), then App Review, and "
        "confirm the professional (Business) account."),
    "x": (
        "Top up X API credits in console.x.com, then re-create held "
        "drafts (held drafts cannot be re-posted)."),
    "tiktok": (
        "Await TikTok's app-review outcome, then pass the separate "
        "Content Posting audit — until then posts can only be private "
        "(SELF_ONLY)."),
    "reddit": (
        "Create a Reddit Ads account, then a developer app in Ads "
        "Manager (Business > Developer Portal)."),
    "linkedin": (
        "Create a LinkedIn developer app, then apply for Standard Tier "
        "(access form + screencast demo + test credentials)."),
    "youtube": (
        "Create a Google Cloud project with an API key / OAuth client; "
        "public uploads need the YouTube API audit."),
    "pinterest": (
        "Create a Pinterest Business account, then move from Trial to "
        "Standard access (video demo)."),
}


def _state(channel: str, state: str, reason: str, *,
            unblock_step: str = "", ever_posted: bool = False) -> dict:
    return {
        "channel": channel,
        "display_name": _DISPLAY_NAMES[channel],
        "state": state,
        "reason": reason,
        "unblock_step": unblock_step,
        "ever_posted": ever_posted,
        "publish_path": _PUBLISH_PATHS[channel],
    }


def channel_state(store: SaasStore, config: SaasConfig, site_id: str,
                  channel: str, env=None) -> dict:
    """The live per-channel state for one site, derived from the
    store + config. See the module docstring for the derivation
    order. ``env`` is the environment mapping TikTok's operator-app
    state is read from (None reads the real environment)."""
    if channel not in CHANNELS:
        raise ValueError(f"Unknown publishing channel: {channel}")
    kind = _STORE_KINDS[channel]
    ever_posted = store.has_successful_post(site_id, channel)

    # Channels with no connection kind in the store at all can only
    # ever be not_configured — there is nothing to connect.
    if kind is None:
        reasons = {
            "linkedin": ("Company Page 'Road to CISSP' exists, but "
                         "there is no LinkedIn developer app."),
            "youtube": ("Channel @road2cissp exists, but there is no "
                        "Google Cloud project / API key / OAuth client."),
            "pinterest": "No Pinterest Business account.",
        }
        return _state(channel, NOT_CONFIGURED, reasons[channel],
                      unblock_step=_UNBLOCK_STEPS[channel],
                      ever_posted=ever_posted)

    # 1. Operator app / config missing -> not_configured.
    if channel in ("facebook", "instagram"):
        if not config.facebook_configured:
            return _state(
                channel, NOT_CONFIGURED,
                "The Meta app id and secret are not configured yet.",
                unblock_step=("Operator step: set VENTORYN_FACEBOOK_APP_ID "
                              "and VENTORYN_FACEBOOK_APP_SECRET (the Meta "
                              "app id/secret)."),
                ever_posted=ever_posted)
    elif channel == "x":
        if not (config.x_app_registered and config.x_oauth_configured):
            return _state(
                channel, NOT_CONFIGURED,
                "The X app is not registered, or its OAuth credentials "
                "are not configured.",
                unblock_step=("Operator step: register Axionory's X app in "
                              "X's developer console and set "
                              "VENTORYN_X_CLIENT_ID / "
                              "VENTORYN_X_CLIENT_SECRET."),
                ever_posted=ever_posted)
    elif channel == "tiktok":
        if not _tiktok.configured(env):
            return _state(
                channel, NOT_CONFIGURED,
                "The TikTok client key and secret are not configured.",
                unblock_step=("Operator step: set TIKTOK_CLIENT_KEY and "
                              "TIKTOK_CLIENT_SECRET."),
                ever_posted=ever_posted)
    elif channel == "reddit":
        if not config.reddit_app_registered:
            return _state(
                channel, NOT_CONFIGURED,
                "No Reddit account / developer app.",
                unblock_step=_UNBLOCK_STEPS["reddit"],
                ever_posted=ever_posted)

    # 2. Known provider-side gate not cleared -> blocked.
    if channel in ("facebook", "instagram"):
        if not config.meta_business_verified:
            return _state(
                channel, BLOCKED,
                "Meta business verification step 3 pending.",
                unblock_step=_UNBLOCK_STEPS[channel],
                ever_posted=ever_posted)
    elif channel == "x":
        if not config.x_credits_available:
            return _state(
                channel, BLOCKED,
                "X API posts fail HTTP 402 Payment Required — the "
                "prepaid credit balance is zero.",
                unblock_step=_UNBLOCK_STEPS["x"],
                ever_posted=ever_posted)
    elif channel == "tiktok":
        if not _tiktok.audited(env):
            return _state(
                channel, BLOCKED,
                "TikTok Content Posting app in review since Nov 12, "
                "2026; after approval, a separate Content Posting "
                "audit is needed before public posts.",
                unblock_step=_UNBLOCK_STEPS["tiktok"],
                ever_posted=ever_posted)

    # 3. Site connection not connected -> not_configured.
    connection = store.get_connection(site_id, kind)
    if not connection or connection.get("status") != "connected":
        labels = {
            "facebook": ("No Facebook Page connected for this site.",
                         "Connect your Facebook Page."),
            "instagram": ("No Instagram Business account connected for "
                          "this site.",
                          "Connect your Instagram Business account."),
            "x": ("No X account connected for this site.",
                  "Connect your X account."),
            "tiktok": ("TikTok is not connected for this site — Spencer "
                       "disconnected it; leave disconnected.",
                       "Spencer reconnects TikTok on his word only — do "
                       "not reconnect unasked."),
            "reddit": ("No Reddit account connected for this site.",
                       "Connect your Reddit account."),
        }
        reason, step = labels[channel]
        return _state(channel, NOT_CONFIGURED, reason,
                      unblock_step=step, ever_posted=ever_posted)

    # 4. No publish path built in Axionory yet -> blocked.
    if _PUBLISH_PATHS[channel] is None:
        return _state(
            channel, BLOCKED,
            f"Connected, but Axionory has no "
            f"{_DISPLAY_NAMES[channel]} publish path yet.",
            unblock_step=(f"A {_DISPLAY_NAMES[channel]} publish path has "
                          "to be built before anything can go out on "
                          f"{_DISPLAY_NAMES[channel]}."),
            ever_posted=ever_posted)

    # 5. Connected, gates cleared, path exists -> ready.
    return _state(
        channel, READY,
        f"Connected — a human-approved draft may go out on "
        f"{_DISPLAY_NAMES[channel]}.",
        ever_posted=ever_posted)


def channel_states_for_site(store: SaasStore, config: SaasConfig,
                            site_id: str, env=None) -> list[dict]:
    """Every channel's live state for one site, in display order."""
    return [channel_state(store, config, site_id, channel, env=env)
            for channel in CHANNELS]


def check_publish_allowed(store: SaasStore, config: SaasConfig,
                          site_id: str, channel: str,
                          env=None) -> tuple[bool, str]:
    """The publish gate: (True, reason) only when the channel is
    ``ready`` and social publishing is enabled operator-wide.
    Anything else returns (False, the plain-language reason) — the
    caller shows it and posts nothing."""
    if not config.social_publishing_enabled:
        return False, ("Social publishing is off — the operator has not "
                       "enabled it yet. Nothing was posted.")
    state = channel_state(store, config, site_id, channel, env=env)
    if state["state"] != READY:
        reason = state["reason"]
        if state["unblock_step"]:
            reason = f"{reason} {state['unblock_step']}"
        return False, f"{reason} Nothing was posted."
    return True, state["reason"]


class PublishingQueue:
    """The per-site publishing queue, backed by the SaasStore.

    ``env`` is the environment mapping TikTok's operator-app state
    is read from (None reads the real environment, as in production).
    The queue never calls a provider and never sends anything on its
    own: ``request_publish`` only delegates to an injected publish
    function — the existing per-channel post path — and only when
    the channel is ``ready``."""

    def __init__(self, store: SaasStore, config: SaasConfig,
                 env=None) -> None:
        self.store = store
        self.config = config
        self.env = env

    # -- intake --------------------------------------------------------
    def enqueue_approved_draft(self, site_id: str,
                               draft_id: str) -> dict | None:
        """Accept one approved draft into the queue, held for its
        channel. Only drafts still waiting (``pending`` /
        ``scheduled``) are accepted — a posted, rejected, held or
        unknown draft returns None. One row per draft: enqueueing
        twice returns the existing row. The recorded reason is the
        channel's live state at enqueue time; reads always
        re-derive the state, never trust the row."""
        draft = self.store.get_social_draft(draft_id)
        if draft is None or draft["site_id"] != site_id:
            return None
        if draft["status"] not in ("pending", "scheduled"):
            return None
        channel = draft["kind"]
        if channel not in CHANNELS:
            return None
        state = channel_state(self.store, self.config, site_id, channel,
                              env=self.env)
        reason = state["reason"]
        if state["unblock_step"]:
            reason = f"{reason} {state['unblock_step']}"
        return self.store.enqueue_publishing_item(
            site_id, draft_id, channel, reason)

    # -- reads ---------------------------------------------------------
    def list_queued(self, site_id: str) -> list[dict]:
        """Queued items for a site, each with its draft and its live
        channel state. Rows whose draft was posted/rejected are
        pruned first (see the store) — the queue only ever shows
        drafts still waiting."""
        out = []
        for item in self.store.list_publishing_items(site_id):
            draft = self.store.get_social_draft(item["draft_id"])
            if draft is None:
                continue
            out.append({
                **item,
                "draft": draft,
                "channel_state": channel_state(
                    self.store, self.config, site_id, item["channel"],
                    env=self.env),
            })
        return out

    def get_item(self, item_id: str) -> dict | None:
        """One queued item with its draft and live channel state,
        or None."""
        item = self.store.get_publishing_item(item_id)
        if item is None:
            return None
        draft = self.store.get_social_draft(item["draft_id"])
        if draft is None:
            return None
        return {**item, "draft": draft,
                "channel_state": channel_state(
                    self.store, self.config, item["site_id"],
                    item["channel"], env=self.env)}

    def remove(self, item_id: str) -> bool:
        """Cancel a queued item. The draft itself is untouched."""
        return self.store.remove_publishing_item(item_id)

    # -- publish (human-triggered only, never automatic) ---------------
    def request_publish(self, item_id: str,
                        publish_fn=None) -> tuple[bool, str]:
        """Publish one queued item through its existing per-channel
        post path (``publish_fn``, which must return
        ``(posted, message)`` like ``post_to_x`` /
        ``post_to_facebook``). A ``blocked`` / ``not_configured``
        channel refuses with a clear reason: the publish function is
        never called and the item stays queued. On a confirmed post
        the item is pruned; on any other failure it stays queued.
        ``publish_fn`` None on a ready channel refuses too — the
        queue has no provider call of its own to fall back on."""
        item = self.get_item(item_id)
        if item is None:
            return False, "Queued item not found."
        draft = item["draft"]
        if draft["status"] not in ("pending", "scheduled"):
            self.store.remove_publishing_item(item_id)
            return False, f"This draft was already {draft['status']}."
        allowed, reason = check_publish_allowed(
            self.store, self.config, item["site_id"], item["channel"],
            env=self.env)
        if not allowed:
            return False, reason
        if publish_fn is None:
            return False, (
                "No publish path is wired for this channel yet — use "
                "the channel's existing send path. Nothing was posted.")
        posted, message = publish_fn()
        if posted:
            self.store.remove_publishing_item(item_id)
        return posted, message
