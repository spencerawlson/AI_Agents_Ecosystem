"""Video / UGC ads: script + storyboard generation and the render-job slot.

Holo ships UGC-style video ads; Axionory had no video generation in the
platform. This module closes the gap as far as it can be closed without
choosing or paying for a provider:

1. Script/storyboard: :func:`generate_video_ad_script` writes a short
   ad (hook, scenes with visuals + voiceover lines, CTA) from a site
   URL and an optional plain-dict ``brand_profile`` (Gap 1's profile
   shape lives on PR #82, not on main, so nothing here imports Gap 1).
   Only brand facts actually supplied are used — an empty profile
   produces a generic script, never invented brand facts. The script
   uses the same contract ``core/video_render.py`` enforces and is
   passed through :func:`core.video_render.validate_script` before it
   is returned, so the writer and the renderer share one gate.
   :func:`storyboard` derives the shot list from a script.
2. Render jobs: :class:`VideoRenderProvider` is the provider interface
   (injected client, no live calls at import, tests use fakes only).
   Jobs live in ``SaasStore`` (``saas_video_ad_jobs``) with the
   lifecycle queued -> rendering -> ready | failed. With NO provider
   configured a job sits honestly at ``awaiting_provider``: nothing is
   rendered, no URL is stored, no fake video exists.
3. Draft-only: a rendered video ad is a draft until a human approves
   it through the existing approval record
   (``store.video_review_status`` / ``set_video_review`` — the same
   record the Approvals page uses for agent-made videos).
   :func:`publish_video_ad` is the only publish gate and it refuses
   without an approval record. It posts nothing itself.

Provider note (researched 2026-11-19, public pricing only): OpenAI
Sora is SHUT DOWN — its API sunset on 2026-09-24, so it is not a
choice. HeyGen (live, API pay-as-you-go, avatar presenter) already
has a key-gated v3 client in ``core/media_providers.py`` and is
wrapped here as :class:`HeyGenProvider` — it reads keys only when
called and is never constructed by default. Runway (live, Gen-4.5
$0.12/s API) has no adapter yet: no key, no decision, no code. No
provider is wired in and no video can render until Spencer chooses
one and approves the cost. See docs/video.md.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Protocol

from core.video_render import validate_script

DEFAULT_SCENES = 4
DEFAULT_SCENE_S = 6
DEFAULT_DURATION_S = DEFAULT_SCENES * DEFAULT_SCENE_S  # 24s

#: Statuses a video-ad render job moves through. ``awaiting_provider``
#: is not a status — it is the ``provider_status`` of a queued job
#: with no provider configured (see ``start_render``).
STATUSES = ("queued", "rendering", "ready", "failed")
_TRANSITIONS = {
    "queued": {"rendering", "failed"},
    "rendering": {"ready", "failed"},
    "ready": set(),
    "failed": set(),
}

#: Brand-profile keys honoured by the script writer. Everything else
#: in the dict is ignored — the writer never guesses from it.
_PROFILE_KEYS = ("name", "tagline", "tone", "audience", "offer", "cta")


def _profile(brand_profile) -> dict:
    """Only the recognised string keys of a plain-dict profile, stripped."""
    out = {}
    if isinstance(brand_profile, dict):
        for key in _PROFILE_KEYS:
            value = brand_profile.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
    return out


def _host(site_url: str) -> str:
    if not isinstance(site_url, str) or not site_url.strip():
        raise ValueError("site_url must be a URL with a host")
    parsed = urllib.parse.urlparse(site_url.strip())
    host = parsed.netloc or urllib.parse.urlparse("//" + site_url.strip()).netloc
    host = host.split("@")[-1].split(":")[0].lower()
    if not host or not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", host) \
            or ("." not in host and host != "localhost"):
        raise ValueError("site_url must be a URL with a host")
    return host


def generate_video_ad_script(site_url: str, brand_profile=None, *,
                            duration_s: int = DEFAULT_DURATION_S) -> dict:
    """Write a short video-ad script for a site.

    ``brand_profile`` is an optional plain dict (``name``, ``tagline``,
    ``tone``, ``audience``, ``offer``, ``cta``). Only values actually
    present are used: with no profile the script names only the site
    host — a fact from the URL — and uses generic benefit lines. It
    never invents an offer, a price, a product, or a testimonial.
    The script passes ``core.video_render.validate_script`` before it
    is returned; a failure raises ``ValueError`` with the reasons.
    """
    host = _host(site_url)
    profile = _profile(brand_profile)
    name = profile.get("name", host)
    tone = profile.get("tone", "")
    audience = profile.get("audience", "")
    offer = profile.get("offer", "")
    cta = profile.get("cta", f"Visit {host}")
    tagline = profile.get("tagline", "")

    if tagline:
        hook = tagline if len(tagline) <= 140 else tagline[:137] + "..."
    elif audience:
        hook = f"Made for {audience} — meet {name}."
    else:
        hook = f"Meet {name}."
    if len(hook) > 140:
        hook = hook[:137] + "..."

    def _on_screen(text: str) -> str:
        # On-screen text must fit a phone screen (max 140 chars, the
        # same gate core/video_render enforces); the full line stays
        # in the narration, which has no length cap.
        return text if len(text) <= 140 else text[:137] + "..."

    scene_s = max(3, min(15, duration_s // DEFAULT_SCENES))
    scenes = [
        {
            "on_screen_text": hook,
            "narration": hook,
            "duration_s": scene_s,
            "visual": ("Close-up, phone in hand, thumb scrolling — "
                       f"the {name} homepage fills the screen."),
        },
        {
            "on_screen_text": _on_screen(offer) if offer else "Browse, compare, and pick what fits.",
            "narration": (offer if offer
                          else "Browse, compare, and pick what fits — "
                               "all in one place."),
            "duration_s": scene_s,
            "visual": ("Quick cuts of the site in use: browsing, "
                       "comparing, choosing."),
        },
        {
            "on_screen_text": _on_screen(f"Made for {audience}.") if audience else "Made for real life.",
            "narration": (f"Made for {audience}." if audience
                          else "Made for real life — quick to use, "
                               "easy to come back to."),
            "duration_s": scene_s,
            "visual": ("A real moment of use: on the couch, on the "
                       "bus, at the kitchen table."),
        },
        {
            "on_screen_text": _on_screen(cta),
            "narration": cta,
            "duration_s": scene_s,
            "visual": (f"The {name} homepage again, the call to action "
                       "front and centre."),
        },
    ]
    script = {
        "title": f"{name} — video ad ({scene_s * DEFAULT_SCENES}s)",
        "hook": hook,
        "cta": cta,
        "tone": tone,
        "scenes": scenes,
    }
    errors = validate_script(script)
    if errors:
        raise ValueError("generated script failed validation: "
                         + "; ".join(errors))
    return script


def storyboard(script: dict) -> list[dict]:
    """The shot list of a script: one shot per scene, with timings."""
    if not isinstance(script, dict) or not isinstance(script.get("scenes"), list):
        raise ValueError("script must be a dict with a scenes list")
    shots = []
    start = 0.0
    for i, scene in enumerate(script["scenes"], 1):
        duration = float(scene.get("duration_s", 5))
        shots.append({
            "shot": i,
            "start_s": start,
            "duration_s": duration,
            "visual": scene.get("visual", ""),
            "on_screen_text": scene.get("on_screen_text", ""),
            "voiceover": scene.get("narration", ""),
        })
        start += duration
    return shots


def total_duration_s(script: dict) -> float:
    return sum(float(s.get("duration_s", 5)) for s in script.get("scenes", []))


# -- render providers ---------------------------------------------------------

class VideoRenderProvider(Protocol):
    """A video-render provider. Implementations are injected; nothing
    here makes a live call. ``render`` returns ``{"video_url": ...}``
    when the video is done, or ``{"provider_job_id": ...}`` when the
    provider renders asynchronously — then :func:`poll_render` asks
    :meth:`status` until it is ready or failed. Any failure raises."""

    name: str

    def render(self, script: dict, *, aspect: str = "vertical") -> dict: ...

    def status(self, provider_job_id: str) -> dict: ...


class HeyGenProvider:
    """HeyGen v3 avatar presenter, wrapping the key-gated client in
    ``core/media_providers.py``. Keys are read only when a render is
    attempted; without ``HEYGEN_API_KEY`` / ``HEYGEN_AVATAR_ID`` on
    the server it raises ``MediaProviderError`` with the reason — the
    job is then recorded as failed, never as a fake success."""

    name = "heygen"

    def __init__(self, *, avatar_id: str = "", aspect_ratio: str = "9:16"):
        self.avatar_id = avatar_id
        self.aspect_ratio = aspect_ratio

    def render(self, script: dict, *, aspect: str = "vertical") -> dict:
        from core.media_providers import heygen_create

        lines = " ".join(
            str(s.get("narration", "")).strip()
            for s in script.get("scenes", []))
        video_id = heygen_create(
            lines, aspect_ratio=self.aspect_ratio,
            title=str(script.get("title", ""))[:100],
            avatar_id=self.avatar_id)
        return {"provider_job_id": video_id}

    def status(self, provider_job_id: str) -> dict:
        from core.media_providers import heygen_status

        data = heygen_status(provider_job_id)
        state = str(data.get("status") or "")
        if state == "completed" and data.get("video_url"):
            return {"status": "ready", "video_url": data["video_url"]}
        if state == "failed":
            return {"status": "failed",
                    "error": str(data.get("failure_message")
                                 or data.get("failure_code")
                                 or "HeyGen could not make the video")}
        return {"status": "rendering"}


# -- render-job lifecycle -----------------------------------------------------

def create_video_ad_job(store, account_id: str, site_id: str,
                        script: dict) -> dict:
    """Queue a video-ad render job. The script is validated first —
    an invalid script is refused, never queued."""
    errors = validate_script(script)
    if errors:
        raise ValueError("script failed validation: " + "; ".join(errors))
    return store.create_video_ad_job(account_id, site_id, script)


def _move(store, job: dict, status: str, **fields) -> dict:
    if status not in _TRANSITIONS.get(job["status"], set()):
        raise ValueError(
            f"video ad job {job['id']} cannot move "
            f"{job['status']} -> {status}")
    return store.update_video_ad_job(job["id"], status=status, **fields)


def start_render(store, job_id: str,
                 provider: VideoRenderProvider | None = None) -> dict:
    """Start rendering a queued job.

    No provider (the state of the platform today) -> the job stays
    queued with ``provider_status="awaiting_provider"``. No render
    call is made, no URL is stored, nothing is faked. With a provider
    -> ``rendering``; a synchronous ``video_url`` moves it straight to
    ``ready``, an async ``provider_job_id`` leaves it rendering for
    :func:`poll_render`, and any provider exception records ``failed``
    with the reason.
    """
    job = store.get_video_ad_job(job_id)
    if job is None:
        raise ValueError(f"video ad job not found: {job_id}")
    if job["status"] != "queued":
        raise ValueError(
            f"video ad job {job['id']} is {job['status']}, not queued")
    if provider is None:
        return store.update_video_ad_job(
            job_id, provider_status="awaiting_provider")
    job = _move(store, job, "rendering", provider=provider.name,
                provider_status="rendering")
    try:
        result = provider.render(job["script"])
    except Exception as exc:  # noqa: BLE001 - provider failure is a job outcome
        return _move(store, job, "failed", provider_status="failed",
                     error=str(exc) or exc.__class__.__name__)
    if result.get("video_url"):
        url = str(result["video_url"])
        if not url.startswith("https://"):
            return _move(store, job, "failed", provider_status="failed",
                         error="provider returned an unexpected video address")
        return _move(store, job, "ready", provider_status="ready",
                     video_url=url)
    if result.get("provider_job_id"):
        return store.update_video_ad_job(
            job_id, provider_job_id=str(result["provider_job_id"]))
    return _move(store, job, "failed", provider_status="failed",
                 error="provider returned neither a video URL nor a job id")


def poll_render(store, job_id: str,
                provider: VideoRenderProvider) -> dict:
    """Advance an async rendering job via the provider's status."""
    job = store.get_video_ad_job(job_id)
    if job is None:
        raise ValueError(f"video ad job not found: {job_id}")
    if job["status"] != "rendering" or not job.get("provider_job_id"):
        return job
    result = provider.status(job["provider_job_id"])
    state = result.get("status")
    if state == "ready" and result.get("video_url"):
        url = str(result["video_url"])
        if not url.startswith("https://"):
            return _move(store, job, "failed", provider_status="failed",
                         error="provider returned an unexpected video address")
        return _move(store, job, "ready", provider_status="ready",
                     video_url=url)
    if state == "failed":
        return _move(store, job, "failed", provider_status="failed",
                     error=str(result.get("error") or "provider render failed"))
    return job


# -- draft-only publish gate --------------------------------------------------

def publish_video_ad(store, job_id: str) -> tuple[bool, str]:
    """The only publish gate for a video ad. Returns ``(True, ...)``
    only when the job is ready with a real video URL AND a human has
    approved it (the ``saas_video_reviews`` record the Approvals page
    writes). Anything else is refused with the reason. This function
    posts nothing — actual posting stays with the existing
    approved-video flow; it certifies the gate, nothing more."""
    job = store.get_video_ad_job(job_id)
    if job is None:
        return False, "Video ad not found."
    if job["status"] != "ready" or not job.get("video_url"):
        return False, "Video ad is not ready yet."
    if store.video_review_status(job_id) != "approved":
        return False, ("Approve this video ad in Approvals first; "
                       "only approved video ads can be posted.")
    return True, "Approved — ready to post."
