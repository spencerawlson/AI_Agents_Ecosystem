"""Shared, durable state for businesses and the ledger (SQLite).

Until now every ecosystem process kept its own in-memory registry and
ledger: the dashboard's game view, the worker ticks and the missions
each lived in a separate world, and a restart wiped the portfolio. That
made real businesses invisible in the GUI — a mission could run a real
growth cycle for a real site and the game screen never showed it.

These two classes implement the exact public interfaces of
core.registry.BusinessRegistry and core.ledger.Ledger, backed by one
SQLite file (stdlib only, WAL mode). Every read goes to the database,
so a business or ledger entry written by ANY process — a mission, the
dashboard, a worker — is visible to all the others immediately, and
survives restarts.

Opt-in: build_runtime uses these when ECOSYSTEM_STATE_DB names the
database file. Without it, the in-memory classes remain the default
(tests stay hermetic; nothing changes for existing callers).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from orchestrator.models import utcnow

from .ledger import KIND_TO_FIELD, Ledger, ProfitAndLoss
from .models import TRANSITIONS, Business, BusinessStatus, LedgerEntry
from .registry import BusinessRegistry, InvalidTransition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id TEXT PRIMARY KEY,
    created_seq INTEGER,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL,
    created_seq INTEGER,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_business ON ledger_entries(business_id);
"""


class _SQLiteStore:
    """One connection per instance; SQLite itself arbitrates between
    processes (WAL + busy timeout). A lock serializes this process's
    threads (the dashboard serves requests concurrently)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=10.0)
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SharedBusinessRegistry(BusinessRegistry):
    """BusinessRegistry with the same semantics, SQLite-backed."""

    def __init__(self, db_path: str | Path) -> None:
        self._store = _SQLiteStore(db_path)

    # -- persistence helpers -------------------------------------------------
    def _save(self, biz: Business) -> None:
        data = biz.model_dump_json()
        with self._store._lock, self._store._conn:
            cur = self._store._conn.execute(
                "SELECT created_seq FROM businesses WHERE id = ?",
                (biz.id,))
            row = cur.fetchone()
            if row is None:
                self._store._conn.execute(
                    "INSERT INTO businesses (id, created_seq, data) "
                    "VALUES (?, (SELECT COALESCE(MAX(created_seq), 0) + 1 "
                    "FROM businesses), ?)", (biz.id, data))
            else:
                self._store._conn.execute(
                    "UPDATE businesses SET data = ? WHERE id = ?",
                    (data, biz.id))

    def _load(self, business_id: str) -> Business | None:
        with self._store._lock:
            cur = self._store._conn.execute(
                "SELECT data FROM businesses WHERE id = ?", (business_id,))
            row = cur.fetchone()
        return Business.model_validate_json(row[0]) if row else None

    def _load_all(self) -> list[Business]:
        with self._store._lock:
            cur = self._store._conn.execute(
                "SELECT data FROM businesses ORDER BY created_seq")
            rows = cur.fetchall()
        return [Business.model_validate_json(r[0]) for r in rows]

    # -- public API (mirrors BusinessRegistry) --------------------------------
    def create(self, name: str, business_type: str, **kwargs) -> Business:
        biz = Business(name=name, business_type=business_type, **kwargs)
        self._save(biz)
        return biz

    def get(self, business_id: str) -> Business | None:
        return self._load(business_id)

    def list(self, status: BusinessStatus | None = None) -> list[Business]:
        businesses = self._load_all()
        if status is not None:
            businesses = [b for b in businesses if b.status == status]
        return sorted(businesses, key=lambda b: b.created_at)

    def transition(self, business_id: str, to: BusinessStatus) -> Business:
        biz = self._load(business_id)
        if biz is None:
            raise ValueError(f"unknown business: {business_id}")
        allowed = list(TRANSITIONS.get(biz.status, []))
        # Termination is always allowed except from already-terminal states.
        if to == BusinessStatus.TERMINATED and biz.status not in (
            BusinessStatus.MATURE,
            BusinessStatus.TERMINATED,
        ):
            allowed.append(BusinessStatus.TERMINATED)
        if to not in allowed:
            raise InvalidTransition(f"cannot transition {biz.status} -> {to}")
        biz.status = to
        biz.updated_at = utcnow()
        self._save(biz)
        return biz


class SharedLedger(Ledger):
    """Ledger with the same semantics, SQLite-backed."""

    def __init__(self, db_path: str | Path) -> None:
        self._store = _SQLiteStore(db_path)

    def record(
        self,
        business_id: str,
        kind: str,
        amount_usd: float,
        description: str = "",
    ) -> LedgerEntry:
        entry = LedgerEntry(
            business_id=business_id, kind=kind,
            amount_usd=amount_usd, description=description)
        with self._store._lock, self._store._conn:
            self._store._conn.execute(
                "INSERT INTO ledger_entries (id, business_id, created_seq, "
                "data) VALUES (?, ?, (SELECT COALESCE(MAX(created_seq), 0) "
                "+ 1 FROM ledger_entries), ?)",
                (entry.id, business_id, entry.model_dump_json()))
        return entry

    def entries(self, business_id: str) -> list[LedgerEntry]:
        with self._store._lock:
            cur = self._store._conn.execute(
                "SELECT data FROM ledger_entries WHERE business_id = ? "
                "ORDER BY created_seq", (business_id,))
            rows = cur.fetchall()
        return [LedgerEntry.model_validate_json(r[0]) for r in rows]

    def pnl(self, business_id: str) -> ProfitAndLoss:
        pnl = ProfitAndLoss()
        for entry in self.entries(business_id):
            field = KIND_TO_FIELD.get(entry.kind, "other")
            setattr(pnl, field, getattr(pnl, field) + abs(entry.amount_usd))
        return pnl


def shared_state_enabled() -> str | None:
    """The configured shared-state database path, or None."""
    import os
    return os.environ.get("ECOSYSTEM_STATE_DB") or None
