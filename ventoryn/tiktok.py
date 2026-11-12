"""TikTok: connect a website's TikTok account (Login Kit) and publish
owner-approved videos (Content Posting API).

Endpoints checked against developers.tiktok.com on 2026-11-12:
- authorize  https://www.tiktok.com/v2/auth/authorize/  (client_key,
  response_type=code, scope comma-separated, redirect_uri, state)
- user info  GET  /v2/user/info/?fields=open_id,avatar_url,display_name
- creator    POST /v2/post/publish/creator_info/query/
- direct     POST /v2/post/publish/video/init/        (video.publish)
- inbox      POST /v2/post/publish/inbox/video/init/  (video.upload)
- status     POST /v2/post/publish/status/fetch/
  status: PROCESSING_UPLOAD | PROCESSING_DOWNLOAD | SEND_TO_USER_INBOX |
          PUBLISH_COMPLETE | FAILED  (fail_reason)
Not readable in the docs at the time (403): the token and revoke pages.
They use TikTok's v2 form here (POST /v2/oauth/token/ and
/v2/oauth/revoke/, form-encoded client_key, client_secret, …); the
first real connection confirms them and any error is shown as is.
Tokens: access 2 hours, refresh 180 days and ROTATED on every refresh
(the new refresh token must replace the old one).

Videos are pulled by TikTok from our verified domain through a signed,
short-lived URL (PULL_FROM_URL): https://<base>/media/tiktok/<token>.mp4
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com"
TOKEN_URL = f"{API}/v2/oauth/token/"
REVOKE_URL = f"{API}/v2/oauth/revoke/"
USER_INFO_URL = f"{API}/v2/user/info/?fields=open_id,avatar_url,display_name"
CREATOR_URL = f"{API}/v2/post/publish/creator_info/query/"
DIRECT_URL = f"{API}/v2/post/publish/video/init/"
INBOX_URL = f"{API}/v2/post/publish/inbox/video/init/"
STATUS_URL = f"{API}/v2/post/publish/status/fetch/"
SCOPES = "user.info.basic,video.publish,video.upload"
PRIVACY_LABELS = {
    "PUBLIC_TO_EVERYONE": "Everyone",
    "MUTUAL_FOLLOW_FRIENDS": "Friends",
    "FOLLOWER_OF_CREATOR": "Followers",
    "SELF_ONLY": "Only me",
}
MEDIA_TTL_S = 2 * 3600


class TikTokError(RuntimeError):
    """A refusal or failure from TikTok; the message is safe to show."""


def _env(name, env=None) -> str:
    return ((env if env is not None else os.environ).get(name) or "").strip()


def configured(env=None) -> bool:
    return bool(_env("TIKTOK_CLIENT_KEY", env) and _env("TIKTOK_CLIENT_SECRET", env))


def redirect_uri(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/account/auth/tiktok/callback"


def authorize_url(base_url: str, state: str, env=None) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_key": _env("TIKTOK_CLIENT_KEY", env), "response_type": "code",
        "scope": SCOPES, "redirect_uri": redirect_uri(base_url), "state": state})


def _call(url: str, *, data: dict | None = None, form: dict | None = None,
          token: str = "", urlopen=None) -> dict:
    headers = {}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json; charset=UTF-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body is not None or url in (
                                     CREATOR_URL,) else "GET")
    try:
        with (urlopen or urllib.request.urlopen)(req, timeout=30) as resp:
            out = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            out = json.loads(exc.read() or b"{}")
        except Exception:  # noqa: BLE001
            raise TikTokError(f"TikTok answered HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise TikTokError(f"could not reach TikTok ({exc.reason})") from None
    err = out.get("error")
    if isinstance(err, dict) and err.get("code") not in (None, "", "ok"):
        raise TikTokError(f"TikTok: {err.get('message') or err.get('code')}")
    if isinstance(err, str) and err:  # OAuth endpoints use error/error_description
        raise TikTokError(f"TikTok: {out.get('error_description') or err}")
    return out


def _tokens(out: dict) -> dict:
    if not out.get("access_token"):
        raise TikTokError("TikTok did not return an access token")
    now = int(time.time())
    return {"access_token": out["access_token"],
            "refresh_token": out.get("refresh_token", ""),
            "open_id": out.get("open_id", ""), "scope": out.get("scope", ""),
            "expires_at": now + int(out.get("expires_in") or 0),
            "refresh_expires_at": now + int(out.get("refresh_expires_in") or 0)}


def exchange_code(base_url: str, code: str, env=None, urlopen=None) -> dict:
    return _tokens(_call(TOKEN_URL, urlopen=urlopen, form={
        "client_key": _env("TIKTOK_CLIENT_KEY", env),
        "client_secret": _env("TIKTOK_CLIENT_SECRET", env),
        "code": code, "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(base_url)}))


def refresh(refresh_token: str, env=None, urlopen=None) -> dict:
    """New tokens; TikTok rotates the refresh token — store the new one."""
    return _tokens(_call(TOKEN_URL, urlopen=urlopen, form={
        "client_key": _env("TIKTOK_CLIENT_KEY", env),
        "client_secret": _env("TIKTOK_CLIENT_SECRET", env),
        "grant_type": "refresh_token", "refresh_token": refresh_token}))


def revoke(access_token: str, env=None, urlopen=None) -> None:
    _call(REVOKE_URL, urlopen=urlopen, form={
        "client_key": _env("TIKTOK_CLIENT_KEY", env),
        "client_secret": _env("TIKTOK_CLIENT_SECRET", env), "token": access_token})


def user_info(access_token: str, urlopen=None) -> dict:
    out = _call(USER_INFO_URL, token=access_token, urlopen=urlopen)
    user = (out.get("data") or {}).get("user") or {}
    return {"open_id": user.get("open_id", ""), "display_name": user.get("display_name", ""),
            "avatar_url": user.get("avatar_url", "")}


def creator_info(access_token: str, urlopen=None) -> dict:
    return _call(CREATOR_URL, data={}, token=access_token, urlopen=urlopen).get("data") or {}


def publish(access_token: str, video_url: str, *, mode: str, post_info: dict | None = None,
            urlopen=None) -> str:
    """Start a post: mode "direct" (published to the profile) or "inbox"
    (a draft the creator finishes in the TikTok app). Returns publish_id."""
    source = {"source": "PULL_FROM_URL", "video_url": video_url}
    if mode == "direct":
        body = {"post_info": post_info or {}, "source_info": source}
        url = DIRECT_URL
    else:
        body = {"source_info": source}
        url = INBOX_URL
    pid = (_call(url, data=body, token=access_token, urlopen=urlopen).get("data") or {}).get("publish_id")
    if not pid:
        raise TikTokError("TikTok did not return a publish id")
    return pid


def status(access_token: str, publish_id: str, urlopen=None) -> dict:
    return _call(STATUS_URL, data={"publish_id": publish_id}, token=access_token,
                 urlopen=urlopen).get("data") or {}


# -- signed media URL for PULL_FROM_URL --------------------------------------

def _media_key(env=None) -> bytes:
    secret = _env("TIKTOK_CLIENT_SECRET", env) or _env("VENTORYN_TOKEN_KEY", env)
    return hashlib.sha256(b"axionory-tiktok-media|" + secret.encode()).digest()


def media_token(job_id: str, now: float | None = None, env=None) -> str:
    exp = int((now or time.time()) + MEDIA_TTL_S)
    msg = f"{job_id}.{exp}"
    sig = hmac.new(_media_key(env), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{msg}.{sig}".encode()).decode().rstrip("=")


def media_job(token: str, now: float | None = None, env=None) -> str | None:
    """The job id a media token grants, or None (bad, forged or expired)."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        job_id, exp, sig = raw.rsplit(".", 2)
    except Exception:  # noqa: BLE001
        return None
    good = hmac.new(_media_key(env), f"{job_id}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(good, sig) or int(exp) < (now or time.time()):
        return None
    return job_id


def fresh_access_token(store, site_id: str, env=None, urlopen=None) -> str:
    """The site's TikTok access token, refreshed (and the rotated refresh
    token saved) when it is about to expire."""
    conn = store.get_connection(site_id, "tiktok") or {}
    raw = dict(conn.get("_detail_raw") or {})
    if conn.get("status") != "connected" or not raw.get("access_token"):
        raise TikTokError("TikTok is not connected for this website")
    if int(raw.get("expires_at") or 0) - 300 > time.time():
        return raw["access_token"]
    new = refresh(raw.get("refresh_token", ""), env=env, urlopen=urlopen)
    raw.update(new)
    store.update_connection(site_id, "tiktok", "connected", conn.get("note", "Connected"), raw)
    return raw["access_token"]


# -- the posting screen (TikTok's required UX for Direct Post) ---------------
# developers.tiktok.com/doc/content-sharing-guidelines: show the creator's
# nickname; privacy picked by the user from creator_info with NO default;
# comment/duet/stitch off by default and greyed out when the creator turned
# them off; a commercial-content disclosure (off by default) with "Your
# brand" / "Branded content"; branded content cannot be private; the
# Music Usage (and Branded Content) confirmation text; a preview; an
# editable title; no watermark; tell the user processing takes minutes.

MUSIC_URL = "https://www.tiktok.com/legal/page/global/music-usage-confirmation/en"
BRANDED_URL = "https://www.tiktok.com/legal/page/global/bc-policy/en"
FINAL_STATES = ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED")
STATUS_WORDS = {
    "PROCESSING_UPLOAD": "TikTok is receiving the video",
    "PROCESSING_DOWNLOAD": "TikTok is downloading the video",
    "SEND_TO_USER_INBOX": "Sent to your TikTok inbox. Open TikTok to finish and post it",
    "PUBLISH_COMPLETE": "Posted on TikTok",
    "FAILED": "TikTok could not post it",
}


def _e(v) -> str:
    import html
    return html.escape(str(v), quote=True)


def audited(env=None) -> bool:
    """TikTok audits an app before it may post publicly; until then every
    post must be private (SELF_ONLY). Set TIKTOK_AUDITED=1 once approved."""
    return _env("TIKTOK_AUDITED", env) == "1"


def _privacy_options(creator: dict, env=None) -> list[str]:
    options = [o for o in (creator.get("privacy_level_options") or []) if o in PRIVACY_LABELS]
    if not audited(env):
        options = [o for o in options if o == "SELF_ONLY"] or ["SELF_ONLY"]
    return options


def post_form_html(*, action: str, video_src: str, creator: dict, totp_field: str,
                   title: str = "", error: str = "", env=None) -> str:
    nick = creator.get("creator_nickname") or ""
    user = creator.get("creator_username") or ""
    priv = "".join(f"<option value='{_e(o)}'>{_e(PRIVACY_LABELS[o])}</option>"
                   for o in _privacy_options(creator, env))
    max_s = creator.get("max_video_post_duration_sec")

    def box(name, label, off):
        dis = " disabled" if off else ""
        why = " <span class='muted'>(turned off in your TikTok settings)</span>" if off else ""
        return (f"<label class='tt-check'><input type='checkbox' name='{name}' value='1'{dis}>"
                f" {label}{why}</label>")
    audit_note = ("" if audited(env) else
                  "<p class='muted tt-hint'>Until TikTok finishes reviewing Axionory, posts can "
                  "only be private (Only me). You can change who sees it later in TikTok.</p>")
    err = f"<p class='tt-err'>{_e(error)}</p>" if error else ""
    handle = f" (@{_e(user)})" if user else ""
    dur = (f"<p class='muted tt-hint'>Your account accepts videos up to {int(max_s)} seconds.</p>"
           if max_s else "")
    music = (f"<a href='{MUSIC_URL}' target='_blank' rel='noopener'>"
             "Music Usage Confirmation</a>")
    bc = f"<a href='{BRANDED_URL}' target='_blank' rel='noopener'>Branded Content Policy</a>"
    css = (".tt-grid{display:grid;grid-template-columns:minmax(160px,260px) 1fr;gap:22px;"
           "align-items:start}.tt-grid video{width:100%;border-radius:12px;background:#000;"
           "aspect-ratio:9/16}.tt-check{display:flex;gap:8px;align-items:center;"
           "font-weight:500;margin:6px 0}.tt-check input{width:auto;margin:0}"
           ".tt-sub{margin:4px 0 0 26px}.tt-err{color:#b91c1c;font-weight:600}"
           ".tt-hint{font-size:14px}fieldset{border:1px solid var(--v-line,#e5e7eb);"
           "border-radius:10px;margin:12px 0;padding:8px 14px}"
           "@media(max-width:620px){.tt-grid{grid-template-columns:1fr}}")
    js = ("(function(){var $=function(i){return document.getElementById(i);};"
          "var c=$('tt-commercial'),opts=$('tt-commercial-opts'),own=$('tt-own'),"
          "br=$('tt-branded'),note=$('tt-label-note'),sub=$('tt-submit'),"
          "priv=$('tt-privacy'),cons=$('tt-consent');"
          f"var music={music!r},bc={bc!r};"
          "function sync(){opts.hidden=!c.checked;"
          "var self=priv.querySelector(\"option[value='SELF_ONLY']\");"
          "if(self){self.disabled=c.checked&&br.checked;"
          "if(self.disabled&&priv.value==='SELF_ONLY'){priv.value='';}"
          "self.title=self.disabled?'Branded content visibility cannot be set to private.':'';}"
          "if(!c.checked){note.textContent='';sub.disabled=false;sub.title='';}"
          "else if(!own.checked&&!br.checked){note.textContent='Choose at least one option.';"
          "sub.disabled=true;sub.title='You need to indicate if your content promotes "
          "yourself, a third party, or both.';}"
          "else{sub.disabled=false;sub.title='';note.textContent=br.checked?"
          "\"Your video will be labeled as 'Paid partnership'.\":"
          "\"Your video will be labeled as 'Promotional content'.\";}"
          "cons.innerHTML=\"By posting, you agree to TikTok's \"+"
          "(c.checked&&br.checked?bc+' and ':'')+music+'.';}"
          "[c,own,br,priv].forEach(function(el){el.addEventListener('change',sync);});"
          "sync();})();")
    return (
        f"<style>{css}</style><div class='card'>{err}"
        f"<p>Posting to TikTok as <strong>{_e(nick)}</strong>{handle}</p>"
        f"<div class='tt-grid'>"
        f"<video controls preload='metadata' playsinline src='{_e(video_src)}'></video>"
        f"<form method='post' action='{_e(action)}' id='tt-form'>"
        f"<label for='tt-title'>Title</label>"
        f"<textarea id='tt-title' name='title' maxlength='2200' rows='4' "
        f"placeholder='Caption, #hashtags and @mentions'>{_e(title)}</textarea>"
        f"<label for='tt-privacy'>Who can view this video</label>"
        f"<select id='tt-privacy' name='privacy' required>"
        f"<option value='' selected disabled>Choose who can see it</option>{priv}</select>"
        f"{audit_note}"
        f"<fieldset><legend>Allow viewers to</legend>"
        f"{box('allow_comment', 'Comment', creator.get('comment_disabled'))}"
        f"{box('allow_duet', 'Duet', creator.get('duet_disabled'))}"
        f"{box('allow_stitch', 'Stitch', creator.get('stitch_disabled'))}</fieldset>"
        f"<fieldset><legend>Content disclosure</legend>"
        f"<label class='tt-check'><input type='checkbox' id='tt-commercial' name='commercial' "
        f"value='1'> This video promotes a brand, product or service</label>"
        f"<div id='tt-commercial-opts' class='tt-sub' hidden>"
        f"<label class='tt-check'><input type='checkbox' id='tt-own' name='brand_organic' "
        f"value='1'> Your brand <span class='muted'>(you are promoting yourself or your own "
        f"business)</span></label>"
        f"<label class='tt-check'><input type='checkbox' id='tt-branded' name='brand_content' "
        f"value='1'> Branded content <span class='muted'>(you are promoting another brand or "
        f"a third party)</span></label>"
        f"<p class='muted tt-hint' id='tt-label-note'></p></div>"
        f"<label class='tt-check'><input type='checkbox' name='is_aigc' value='1' checked> "
        f"Label as AI-generated content</label></fieldset>"
        f"<label for='tt-mode'>How to send it</label><select id='tt-mode' name='mode'>"
        f"<option value='direct' selected>Post it to my TikTok profile now</option>"
        f"<option value='inbox'>Send it to my TikTok inbox as a draft (I finish it in the "
        f"app)</option></select>{dur}{totp_field}"
        f"<p class='muted tt-hint' id='tt-consent'>By posting, you agree to TikTok's "
        f"{music}.</p>"
        f"<p><button class='btn btn-primary' type='submit' id='tt-submit'>Post to TikTok"
        f"</button></p><p class='muted tt-hint'>After you post, TikTok can take a few "
        f"minutes to process the video before it appears on your profile.</p>"
        f"</form></div></div><script>{js}</script>")


def post_info_from_form(form: dict, creator: dict, env=None) -> tuple[dict | None, str]:
    """Validate the posting form against creator_info; returns (post_info, error)."""
    privacy = (form.get("privacy") or "").strip()
    if privacy not in _privacy_options(creator, env):
        return None, "Choose who can view this video."
    title = (form.get("title") or "").strip()
    if len(title) > 2200:
        return None, "The title is longer than TikTok allows (2,200 characters)."
    commercial = form.get("commercial") == "1"
    organic = commercial and form.get("brand_organic") == "1"
    branded = commercial and form.get("brand_content") == "1"
    if commercial and not (organic or branded):
        return None, ("You need to indicate if your content promotes yourself, "
                      "a third party, or both.")
    if branded and privacy == "SELF_ONLY":
        return None, "Branded content visibility cannot be set to private."

    def allowed(key, off):
        return form.get(key) == "1" and not off
    return {
        "title": title, "privacy_level": privacy,
        "disable_comment": not allowed("allow_comment", creator.get("comment_disabled")),
        "disable_duet": not allowed("allow_duet", creator.get("duet_disabled")),
        "disable_stitch": not allowed("allow_stitch", creator.get("stitch_disabled")),
        "brand_organic_toggle": organic, "brand_content_toggle": branded,
        "is_aigc": form.get("is_aigc") == "1",
    }, ""
