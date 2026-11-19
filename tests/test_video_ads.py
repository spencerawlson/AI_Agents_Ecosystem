"""Video / UGC ads: script/storyboard generation, the render-job slot,
the unconfigured-provider wait, and the draft-only publish gate.

No network and no real provider anywhere — render providers are fakes.
No video is rendered, no URL is ever invented: a job with no provider
configured waits at ``awaiting_provider``.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ventoryn.saas import SaasStore
from ventoryn import video_ads
from ventoryn.video_ads import (
    HeyGenProvider,
    create_video_ad_job,
    generate_video_ad_script,
    poll_render,
    publish_video_ad,
    start_render,
    storyboard,
)


@pytest.fixture()
def store():
    return SaasStore(":memory:")


@pytest.fixture()
def site(store):
    account = store.create_account("google-sub-video", "ada@example.com", "Ada")
    return account, store.create_site(account["id"], "https://www.example-shop.com")


def _script(**over):
    script = generate_video_ad_script("https://www.example-shop.com")
    script.update(over)
    return script


class FakeSyncProvider:
    """Renders immediately and returns a real-looking URL."""

    name = "fake-sync"
    calls = 0

    def __init__(self):
        self.calls = 0

    def render(self, script, *, aspect="vertical"):
        self.calls += 1
        return {"video_url": "https://cdn.example.invalid/ads/v1.mp4"}

    def status(self, provider_job_id):
        return {"status": "ready",
                "video_url": "https://cdn.example.invalid/ads/v1.mp4"}


class FakeAsyncProvider:
    """Returns a provider job id first; ready only when polled."""

    name = "fake-async"

    def __init__(self):
        self.calls = 0

    def render(self, script, *, aspect="vertical"):
        self.calls += 1
        return {"provider_job_id": "prov-123"}

    def status(self, provider_job_id):
        assert provider_job_id == "prov-123"
        return {"status": "ready",
                "video_url": "https://cdn.example.invalid/ads/v2.mp4"}


class FakeFailingProvider:
    name = "fake-failing"

    def render(self, script, *, aspect="vertical"):
        raise RuntimeError("provider exploded")

    def status(self, provider_job_id):
        return {"status": "failed", "error": "provider exploded"}


# -- script / storyboard ------------------------------------------------------

def test_script_has_hook_scenes_and_cta():
    script = generate_video_ad_script(
        "https://www.example-shop.com",
        {"name": "Example Shop", "tagline": "Good stuff, fast.",
         "audience": "busy parents", "cta": "Shop Example Shop today"})
    assert script["hook"] == "Good stuff, fast."
    assert script["cta"] == "Shop Example Shop today"
    assert len(script["scenes"]) == 4
    assert sum(s["duration_s"] for s in script["scenes"]) == 24
    for scene in script["scenes"]:
        assert scene["on_screen_text"].strip()
        assert scene["narration"].strip()
        assert scene["visual"].strip()
        assert 3 <= scene["duration_s"] <= 15
    assert "busy parents" in script["scenes"][2]["narration"]


def test_empty_brand_input_produces_generic_script_without_invented_facts():
    for profile in (None, {}, {"name": "   "}, {"offer": ""}):
        script = generate_video_ad_script("https://www.example-shop.com",
                                          profile)
        text = " ".join([script["hook"], script["cta"]] +
                        [s["on_screen_text"] + " " + s["narration"]
                         for s in script["scenes"]]).lower()
        # Only the host (a fact from the URL) may be named; no offer,
        # price, product, discount or testimonial may appear.
        assert "www.example-shop.com" in text
        for invented in ("free shipping", "50% off", "% off", "$",
                         "best seller", "customers love", "review",
                         "guarantee"):
            assert invented not in text, (profile, invented)


def test_bad_url_is_refused():
    with pytest.raises(ValueError):
        generate_video_ad_script("not a url at all :::")


def test_storyboard_is_one_shot_per_scene_with_timings():
    script = generate_video_ad_script("https://www.example-shop.com")
    shots = storyboard(script)
    assert len(shots) == 4
    assert [s["shot"] for s in shots] == [1, 2, 3, 4]
    assert [s["start_s"] for s in shots] == [0.0, 6.0, 12.0, 18.0]
    assert all(s["duration_s"] == 6.0 for s in shots)
    assert shots[0]["voiceover"] == script["scenes"][0]["narration"]
    assert shots[0]["on_screen_text"] == script["scenes"][0]["on_screen_text"]
    assert shots[0]["visual"] == script["scenes"][0]["visual"]


def test_invalid_script_is_never_queued(store, site):
    account, site_row = site
    bad = _script(scenes=[{"on_screen_text": "only one",
                           "narration": "too short", "duration_s": 5}])
    with pytest.raises(ValueError):
        create_video_ad_job(store, account["id"], site_row["id"], bad)
    assert store.list_video_ad_jobs(account["id"]) == []


# -- render-job lifecycle -----------------------------------------------------

def test_job_lifecycle_sync_provider(store, site):
    account, site_row = site
    job = create_video_ad_job(store, account["id"], site_row["id"],
                              _script())
    assert job["status"] == "queued"
    assert job["provider_status"] == "awaiting_provider"
    assert job["video_url"] == ""
    provider = FakeSyncProvider()
    job = start_render(store, job["id"], provider)
    assert job["status"] == "ready"
    assert job["provider_status"] == "ready"
    assert job["video_url"] == "https://cdn.example.invalid/ads/v1.mp4"
    assert provider.calls == 1


def test_job_lifecycle_async_provider_needs_poll(store, site):
    account, site_row = site
    job = create_video_ad_job(store, account["id"], site_row["id"],
                              _script())
    provider = FakeAsyncProvider()
    job = start_render(store, job["id"], provider)
    assert job["status"] == "rendering"
    assert job["provider_job_id"] == "prov-123"
    assert job["video_url"] == ""
    job = poll_render(store, job["id"], provider)
    assert job["status"] == "ready"
    assert job["video_url"] == "https://cdn.example.invalid/ads/v2.mp4"


def test_provider_failure_records_failed_with_reason(store, site):
    account, site_row = site
    job = create_video_ad_job(store, account["id"], site_row["id"],
                              _script())
    job = start_render(store, job["id"], FakeFailingProvider())
    assert job["status"] == "failed"
    assert "provider exploded" in job["error"]
    assert job["video_url"] == ""


def test_finished_job_cannot_render_again(store, site):
    account, site_row = site
    job = create_video_ad_job(store, account["id"], site_row["id"],
                              _script())
    start_render(store, job["id"], FakeSyncProvider())
    with pytest.raises(ValueError):
        start_render(store, job["id"], FakeSyncProvider())


def test_jobs_are_listed_per_site(store, site):
    account, site_row = site
    other = store.create_site(account["id"], "https://www.other-shop.com")
    create_video_ad_job(store, account["id"], site_row["id"], _script())
    create_video_ad_job(store, account["id"], other["id"], _script())
    assert len(store.list_video_ad_jobs(account["id"])) == 2
    assert len(store.list_video_ad_jobs(account["id"],
                                        site_row["id"])) == 1


# -- no provider configured ---------------------------------------------------

def test_unconfigured_provider_job_waits_honestly(store, site):
    account, site_row = site
    job = create_video_ad_job(store, account["id"], site_row["id"],
                              _script())
    provider = FakeSyncProvider()
    job = start_render(store, job["id"], provider=None)
    assert job["status"] == "queued"
    assert job["provider_status"] == "awaiting_provider"
    assert job["video_url"] == ""
    assert job["provider"] == ""
    assert provider.calls == 0  # no render call was made


def test_heygen_adapter_without_keys_fails_honestly_not_fake(store, site):
    account, site_row = site
    job = create_video_ad_job(store, account["id"], site_row["id"],
                              _script())
    job = start_render(store, job["id"],
                       HeyGenProvider(avatar_id="avatar-1"))
    assert job["status"] == "failed"
    assert "HEYGEN_API_KEY" in job["error"]
    assert job["video_url"] == ""


def test_heygen_adapter_submits_and_polls_with_fake_http(monkeypatch):
    import urllib.request

    from core.media_providers import heygen_create, heygen_status

    def fake_urlopen(req, *args, **kwargs):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                if req.full_url.endswith("/v3/videos"):
                    return b'{"data": {"video_id": "vid-9"}}'
                return (b'{"data": {"status": "completed", '
                        b'"video_url": "https://cdn.heygen.invalid/v.mp4"}}')

            @property
            def status(self):
                return 200

        return Resp()

    monkeypatch.setenv("HEYGEN_API_KEY", "key-1")
    monkeypatch.setenv("HEYGEN_AVATAR_ID", "avatar-1")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    provider = HeyGenProvider()
    assert provider.name == "heygen"
    assert provider.render(_script()) == {"provider_job_id": "vid-9"}
    assert provider.status("vid-9") == {
        "status": "ready", "video_url": "https://cdn.heygen.invalid/v.mp4"}
    # The adapter is the same client the repo already tests — the two
    # calls above went through heygen_create / heygen_status.
    assert heygen_create("hello", urlopen=fake_urlopen) == "vid-9"
    assert heygen_status("vid-9", urlopen=fake_urlopen)["status"] == "completed"


# -- draft-only publish gate --------------------------------------------------

def _ready_job(store, site):
    account, site_row = site
    job = create_video_ad_job(store, account["id"], site_row["id"],
                              _script())
    return account, start_render(store, job["id"], FakeSyncProvider())


def test_publish_refused_without_approval_record(store, site):
    _account, job = _ready_job(store, site)
    assert job["status"] == "ready" and job["video_url"]
    ok, message = publish_video_ad(store, job["id"])
    assert ok is False
    assert "Approve" in message


def test_publish_refused_when_not_ready(store, site):
    account, site_row = site
    job = create_video_ad_job(store, account["id"], site_row["id"],
                              _script())
    store.set_video_review(job["id"], account["id"], "approved")
    ok, message = publish_video_ad(store, job["id"])
    assert ok is False
    assert "not ready" in message


def test_publish_allowed_only_when_ready_and_approved(store, site):
    account, job = _ready_job(store, site)
    store.set_video_review(job["id"], account["id"], "approved")
    ok, message = publish_video_ad(store, job["id"])
    assert ok is True
    assert "Approved" in message


def test_publish_refused_after_rejection(store, site):
    account, job = _ready_job(store, site)
    store.set_video_review(job["id"], account["id"], "rejected")
    ok, _message = publish_video_ad(store, job["id"])
    assert ok is False
