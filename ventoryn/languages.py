"""Content languages: which language Axionory generates in, per site.

A site's language is a BCP-47-style tag (`en`, `fr-ca`, `zh-hans`,
`es-419` …) stored on the site row and passed into every generation
prompt — X campaigns, SEO articles, video scripts — so drafts come
back in the language the site's audience reads. No hardcoded list of
languages: any well-formed tag is accepted, and no language count is
claimed anywhere (the models handle the long tail).

Two rules, both honest:

- Empty means "default English". Garbage is rejected with ValueError,
  never silently reinterpreted.
- Brand tone is never machine-translated. A brand profile may carry a
  per-language tone map (`tones: {"fr": [...]}`) alongside its default
  `tone`; when no tone is provided for a language, the default tone is
  used and that is what the code says — the brand's voice in a
  language nobody supplied is not invented here.
"""

from __future__ import annotations

import re

#: The language generation falls back to when a site has none.
DEFAULT_LANGUAGE = "en"

#: Common choices offered in the UI (a datalist — pick one, or type
#: any well-formed tag). A sensible set, not a claim of coverage.
COMMON_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("fr", "French"),
    ("es", "Spanish"),
    ("de", "German"),
    ("pt", "Portuguese"),
    ("it", "Italian"),
    ("nl", "Dutch"),
    ("ht", "Haitian Creole"),
    ("ar", "Arabic"),
    ("zh-hans", "Chinese (Simplified)"),
    ("zh-hant", "Chinese (Traditional)"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("hi", "Hindi"),
    ("ru", "Russian"),
    ("pl", "Polish"),
    ("uk", "Ukrainian"),
    ("tr", "Turkish"),
    ("sv", "Swedish"),
    ("da", "Danish"),
    ("no", "Norwegian"),
    ("fi", "Finnish"),
    ("id", "Indonesian"),
    ("vi", "Vietnamese"),
    ("th", "Thai"),
    ("ms", "Malay"),
)

# BCP-47-style shape, kept deliberately small: a primary subtag of
# 2-3 letters, an optional script subtag (exactly 4 letters), an
# optional region (2 letters or 3 digits), then optional variants
# (5-8 alphanumerics, or a digit followed by 3 alphanumerics).
# Grandfathered / irregular tags (`i-klingon`, `en-GB-oed` with a
# 3-letter variant) are out of scope and rejected — a site that needs
# one can say so, rather than us silently accepting malformed input.
_TAG_RE = re.compile(
    r"[a-z]{2,3}"
    r"(?:-[a-z]{4})?"
    r"(?:-(?:[a-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3})){0,2}"
)

def validate_language(raw: object) -> str | None:
    """A well-formed language tag, lowercased, or None when empty.

    Accepts `en`, `fr-ca`, `zh-hans`, `es-419`, `sr-latn`,
    `de-ch-1901` … — input case does not matter (`EN-CA` → `en-ca`).
    Raises ValueError on anything malformed (`english`, `e`,
    `en--ca`, `en_ca`, `123`). Empty / None means "no choice made":
    the caller decides the default.
    """
    if raw is None:
        return None
    lang = str(raw).strip().lower()
    if not lang:
        return None
    if not _TAG_RE.fullmatch(lang):
        raise ValueError(
            f"{str(raw).strip()!r} is not a language tag — "
            "try one like en, fr-ca or zh-hans")
    return lang


def resolve_language(raw: object, default: str = DEFAULT_LANGUAGE) -> str:
    """The canonical tag to generate in: the validated tag, or the
    default when empty / None. Raises ValueError on a malformed tag
    rather than silently generating in the wrong language."""
    tag = validate_language(raw)
    return tag if tag is not None else default


def primary_subtag(tag: str) -> str:
    """`fr-ca` → `fr`; `zh-hans` → `zh`."""
    return str(tag or "").strip().lower().split("-", 1)[0]


def tone_for_language(profile: dict | None, language: str) -> list[str]:
    """The brand tone to write in for `language`.

    A brand profile (see Gap 1, `brand_dna.extract_profile`) carries
    a default `tone` list and may carry an optional per-language
    `tones` map, e.g. `{"tone": ["warm"], "tones": {"fr": ["chaleureux"]}}`.
    Resolution: exact tag match in `tones`, then primary-subtag match
    (`fr-ca` falls back to the `fr` tone), then the default `tone`.

    If no tone is provided for the language, the default tone is
    returned — never a machine translation of the brand's voice passed
    off as the brand's. A profile with no tone at all yields [].
    """
    profile = profile or {}
    lang = resolve_language(language)
    tones = profile.get("tones") or {}
    if isinstance(tones, dict):
        for key in (lang, primary_subtag(lang)):
            value = tones.get(key)
            if isinstance(value, list) and value:
                return [str(t) for t in value if str(t).strip()]
    tone = profile.get("tone") or []
    if isinstance(tone, list):
        return [str(t) for t in tone if str(t).strip()]
    return []


def language_options(selected: str = DEFAULT_LANGUAGE) -> str:
    """<option> tags for the common languages, with `selected` marked.
    Values are the tags; labels are "Name (tag)"."""
    sel = resolve_language(selected) if str(selected or "").strip() else DEFAULT_LANGUAGE
    out = []
    for code, name in COMMON_LANGUAGES:
        mark = " selected" if code == sel else ""
        out.append(f"<option value='{code}'{mark}>{name} ({code})</option>")
    return "".join(out)


def language_datalist(selected: str = DEFAULT_LANGUAGE) -> str:
    """A <datalist> of the common languages for a free-entry input:
    pick a suggestion, or type any well-formed tag."""
    value = resolve_language(selected) if str(selected or "").strip() else DEFAULT_LANGUAGE
    opts = "".join(
        f"<option value='{code}'>{name} ({code})</option>"
        for code, name in COMMON_LANGUAGES)
    return (f"<datalist id='language-choices'>{opts}</datalist>"
            f"<input type='text' id='language' name='language' "
            f"list='language-choices' value='{value}' maxlength='35' "
            f"placeholder='en' autocomplete='off'>")


def language_instruction(language: str, tone: list[str] | None = None) -> str:
    """One prompt line stating the generation language (and tone,
    when one is known) for a generation prompt."""
    line = f"Write in this language: {language}."
    if tone:
        line += f" Match this brand tone: {', '.join(tone)}."
    return line
