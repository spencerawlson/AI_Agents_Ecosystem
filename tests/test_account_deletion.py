"""Deleting an account leaves nothing behind for its websites.

Before, delete_account removed only connections, conversions and
signatures per site; brand DNA, snapshots, analytics, drafts and the
other site tables stayed. It now runs the same sweep as delete_site
for every website, and another account's data is untouched.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ventoryn import brand_dna, site_snapshot
from ventoryn.analytics import SiteAnalytics
from ventoryn.saas import SaasStore


def _rows_for_site(store, site_id):
    out = {}
    for (t,) in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [c[1] for c in store._conn.execute(f"PRAGMA table_info({t})")]
        if "site_id" in cols:
            n = store._conn.execute(f"SELECT COUNT(*) FROM {t} WHERE site_id = ?",
                                    (site_id,)).fetchone()[0]
            if n:
                out[t] = n
    return out


def _fill(store, account_id, url):
    site = store.create_site(account_id, url)
    dna = brand_dna.brand_dna_store_for(store)
    dna.mark_running(site["id"])
    dna.finish(site["id"], "done", {"name": "x"})
    snap = site_snapshot.snapshot_store_for(store)
    snap.mark_running(site["id"])
    SiteAnalytics(store).record(site, "pageview", url + "/", "", "1.2.3.4", "Mozilla/5.0")
    store.create_social_draft(site["id"], "x", "hello", "")
    return site


def test_account_deletion_removes_every_site_row_and_only_its_own():
    store = SaasStore(":memory:")
    a = store.create_account("sub-a", "a@example.com", "A")
    b = store.create_account("sub-b", "b@example.com", "B")
    site_a = _fill(store, a["id"], "https://a.example.com")
    site_b = _fill(store, b["id"], "https://b.example.com")
    before = _rows_for_site(store, site_a["id"])
    for table in ("saas_brand_dna", "saas_site_snapshots", "saas_social_drafts", "va_events"):
        assert table in before, before
    store.delete_account(a["id"])
    assert _rows_for_site(store, site_a["id"]) == {}
    assert store.get_site(site_a["id"]) is None
    assert _rows_for_site(store, site_b["id"]) == _rows_for_site(store, site_b["id"]) != {}
    assert store.get_site(site_b["id"]) is not None
