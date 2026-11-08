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

from orchestrator.models import AuditEvent, utcnow

from .audit import AuditLog
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
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    created_seq INTEGER,
    data TEXT NOT NULL
);
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

    def assign_owner(self, business_id: str, owner_email: str) -> Business:
        """Bind a business to the account that manages it (persisted)."""
        biz = self._load(business_id)
        if biz is None:
            raise ValueError(f"unknown business: {business_id}")
        biz.owner_email = owner_email.strip().lower()
        biz.updated_at = utcnow()
        self._save(biz)
        return biz

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


class SharedAuditLog(AuditLog):
    """AuditLog with the same semantics, SQLite-backed.

    The bug this fixes: the ledger survived restarts (shared SQLite)
    but the audit log lived only in memory, so a dashboard restart
    emptied "What the team is doing" while the money stayed — and a
    worker process's events never reached the dashboard at all, since
    they died with the worker. Now the audit log lives in the same
    ECOSYSTEM_STATE_DB file as the businesses and the ledger.

    Same pattern as SharedLedger: every read goes to the database, so
    an event recorded by ANY process is visible to the others
    immediately, and survives restarts. The table keeps only the
    latest 500 events so it never grows forever.
    """

    MAX_EVENTS = 500

    def __init__(self, db_path: str | Path) -> None:
        super().__init__()
        self._store = _SQLiteStore(db_path)
        self._events = self._load_all()

    def _load_all(self) -> list[AuditEvent]:
        with self._store._lock:
            cur = self._store._conn.execute(
                "SELECT data FROM audit_events ORDER BY created_seq")
            rows = cur.fetchall()
        return [AuditEvent.model_validate_json(r[0]) for r in rows]

    def record(self, *args, **kwargs) -> AuditEvent:
        evt = super().record(*args, **kwargs)
        with self._store._lock, self._store._conn:
            self._store._conn.execute(
                "INSERT INTO audit_events (id, created_seq, data) "
                "VALUES (?, (SELECT COALESCE(MAX(created_seq), 0) + 1 "
                "FROM audit_events), ?)",
                (evt.id, evt.model_dump_json()))
            self._store._conn.execute(
                "DELETE FROM audit_events WHERE id NOT IN "
                "(SELECT id FROM audit_events ORDER BY created_seq "
                "DESC LIMIT ?)", (self.MAX_EVENTS,))
        if len(self._events) > self.MAX_EVENTS:
            del self._events[:len(self._events) - self.MAX_EVENTS]
        return evt

    def for_business(self, business_id: str) -> list[AuditEvent]:
        return [e for e in self._load_all()
                if e.business_id == business_id]

    def for_task(self, task_id: str) -> list[AuditEvent]:
        return [e for e in self._load_all() if e.task_id == task_id]

    def latest(self, n: int = 50) -> list[AuditEvent]:
        """Most recent events, newest first — read from the database."""
        with self._store._lock:
            cur = self._store._conn.execute(
                "SELECT data FROM audit_events ORDER BY created_seq "
                "DESC LIMIT ?", (n,))
            rows = cur.fetchall()
        return [AuditEvent.model_validate_json(r[0]) for r in rows]

    def all_events(self) -> list[AuditEvent]:
        """Every event, oldest first — read from the database, so a
        worker process's events are visible to the dashboard too."""
        return self._load_all()

    def total_ai_cost(self, business_id: str | None = None) -> float:
        events = self._load_all()
        if business_id is not None:
            events = [e for e in events if e.business_id == business_id]
        return sum(e.cost_usd for e in events)

    def __len__(self) -> int:
        with self._store._lock:
            cur = self._store._conn.execute(
                "SELECT COUNT(*) FROM audit_events")
            row = cur.fetchone()
        return row[0] if row else 0
