"""App-store listing audit: iTunes lookup, listed checks, the
not-listed case, and the audit CLI. No network anywhere here —
every fetch is fake iTunes JSON. The live lookup was verified by
hand once: Kay Ayiti (6826322327) and Farelune (6828243842) both
return resultCount 0, because neither is on the store yet."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.app_store_audit import audit_app, lookup
import ecosystem.app_store_audit as program

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)

GOOD_RECORD = {
    "trackName": "Kay Ayiti",
    "description": ("Find a house in Haiti. Browse listings in Haitian "
                    "Creole, French and English, contact the landlord "
                    "directly, and manage your own listings. Tenants browse "
                    "free; landlords list their houses and land. " * 6),
    "screenshotUrls": ["https://example.com/1.png",
                       "https://example.com/2.png",
                       "https://example.com/3.png",
                       "https://example.com/4.png"],
    "userRatingCount": 12,
    "averageUserRating": 4.5,
    "version": "1.0",
    "currentVersionReleaseDate": "2026-11-01T00:00:00Z",
    "releaseNotes": "First release.",
    "languageCodesISO2A": ["FR", "EN", "HT"],
}


def _fetch(mapping):
    def fake(url):
        return mapping.get(url, (0, ""))
    return fake


def _lookup_url(app_id, country):
    return f"https://itunes.apple.com/lookup?id={app_id}&country={country}"


def _record_fetch(record, app_id="6826322327", country="ca"):
    body = json.dumps({"resultCount": 1, "results": [record]})
    return _fetch({_lookup_url(app_id, country): (200, body)})


# -- lookup -------------------------------------------------------------------

def test_lookup_returns_record_and_storefront():
    result = lookup("6826322327", country="ca",
                    fetch=_record_fetch(GOOD_RECORD))
    assert result["trackName"] == "Kay Ayiti"
    assert result["_storefront"] == "ca"


def test_lookup_falls_back_to_us_storefront():
    empty = json.dumps({"resultCount": 0, "results": []})
    body = json.dumps({"resultCount": 1, "results": [GOOD_RECORD]})
    fetch = _fetch({_lookup_url("6826322327", "ca"): (200, empty),
                    _lookup_url("6826322327", "us"): (200, body)})
    result = lookup("6826322327", country="ca", fetch=fetch)
    assert result is not None
    assert result["_storefront"] == "us"


def test_lookup_not_listed_returns_none():
    empty = json.dumps({"resultCount": 0, "results": []})
    fetch = _fetch({_lookup_url("6826322327", "ca"): (200, empty),
                    _lookup_url("6826322327", "us"): (200, empty)})
    assert lookup("6826322327", country="ca", fetch=fetch) is None


def test_lookup_network_failure_raises():
    with pytest.raises(ValueError, match="network failure"):
        lookup("6826322327", country="ca", fetch=_fetch({}))


# -- audit: listed ------------------------------------------------------------

def test_audit_good_listing_scores_100():
    result = audit_app("6826322327", country="ca",
                       fetch=_record_fetch(GOOD_RECORD), now=NOW)
    assert result["listed"] is True
    assert result["score"] == 100
    assert result["findings"] == []
    assert result["details"]["screenshots"] == 4
    assert result["details"]["ratings"] == 12


def test_audit_thin_listing_flagged():
    thin = dict(GOOD_RECORD, trackName="Kay Ayiti — Maisons et terrains en Haïti pour tous les budgets",
                description="Find a house.",
                screenshotUrls=["https://example.com/1.png"],
                userRatingCount=0, averageUserRating=None,
                releaseNotes="",
                languageCodesISO2A=["FR"],
                currentVersionReleaseDate="2025-01-01T00:00:00Z")
    result = audit_app("6826322327", country="ca",
                       fetch=_record_fetch(thin), now=NOW)
    assert result["score"] < 50
    issues = [f["issue"] for f in result["findings"]]
    assert any("title is" in i for i in issues)
    assert any("description is only" in i for i in issues)
    assert any("only 1 screenshot" in i for i in issues)
    assert any("no ratings yet" in i for i in issues)
    assert any("days old" in i for i in issues)
    assert any("no 'What's New'" in i for i in issues)
    assert any("one language" in i for i in issues)


def test_audit_low_rating_flagged():
    low = dict(GOOD_RECORD, userRatingCount=5, averageUserRating=3.2)
    result = audit_app("6826322327", country="ca",
                       fetch=_record_fetch(low), now=NOW)
    assert any("average rating is 3.2" in f["issue"]
               for f in result["findings"])


# -- audit: not listed --------------------------------------------------------

def test_audit_not_listed_has_no_score():
    empty = json.dumps({"resultCount": 0, "results": []})
    fetch = _fetch({_lookup_url("6826322327", "ca"): (200, empty),
                    _lookup_url("6826322327", "us"): (200, empty)})
    result = audit_app("6826322327", country="ca", fetch=fetch, now=NOW)
    assert result["listed"] is False
    assert result["score"] is None
    assert result["storefronts_checked"] == ["ca", "us"]
    assert any("not on the App Store" in f["issue"]
               for f in result["findings"])
    assert any("title ≤ 30" in f["fix"] for f in result["findings"])


# -- CLI ----------------------------------------------------------------------

def test_cli_list(tmp_path, capsys):
    assert program.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "kayayiti: Apple ID 6826322327" in out
    assert "farelune: Apple ID 6828243842" in out


def test_cli_audit_not_listed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    fake = {"app_id": "6826322327", "listed": False, "score": None,
            "storefronts_checked": ["ca", "us"],
            "findings": [{"severity": "medium", "area": "listing",
                          "issue": "not on the App Store in the storefronts checked",
                          "fix": "Check App Store Connect."}],
            "details": {}, "note": "No public listing exists to score."}
    with patch.object(program, "audit_app", return_value=fake):
        assert program.main(["audit", "--app", "kayayiti"]) == 0
    out = capsys.readouterr().out
    assert "kayayiti: not on the App Store (ca, us)" in out
    report = tmp_path / "reports" / (
        f"app_store_kayayiti_{datetime.now(timezone.utc):%Y%m%d}.md")
    assert report.exists()
    assert "not on the App Store" in report.read_text(encoding="utf-8")


def test_cli_audit_listed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    fake = {"app_id": "6826322327", "listed": True, "score": 92,
            "storefront": "ca",
            "findings": [{"severity": "low", "area": "freshness",
                          "issue": "no 'What's New' notes on this version",
                          "fix": "Write release notes."}],
            "details": {"name": "Kay Ayiti", "version": "1.0",
                        "description_chars": 800, "screenshots": 4,
                        "ratings": 12, "average_rating": 4.5,
                        "languages": ["FR", "EN", "HT"],
                        "current_version_age_days": 6},
            "note": "Keywords are not visible publicly."}
    with patch.object(program, "audit_app", return_value=fake):
        assert program.main(["audit", "--app", "kayayiti"]) == 0
    out = capsys.readouterr().out
    assert "kayayiti: score 92/100" in out
    assert "What's New" in out


def test_cli_audit_needs_app_or_id(capsys):
    with pytest.raises(SystemExit, match="2"):
        program.main(["audit"])
