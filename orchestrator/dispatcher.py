"""Concurrent dispatcher: run agent tasks in parallel across businesses.

The synchronous Orchestrator.dispatch() executes one task at a time. The
Dispatcher instead polls the task store for PENDING tasks, claims them
atomically (so two workers can never execute the same task), and runs
them on a thread pool through the *same* _run_task path: identical
five-layer verification and bounded rework.

Concurrency policy (all configurable):
  - max_workers:      total threads executing tasks
  - max_per_agent:    concurrent tasks per agent type
  - max_per_business: concurrent tasks per business
A task waits in PENDING until a slot frees up.

Approval gates hold: WAITING_APPROVAL tasks are promoted to PENDING only
after their approval is granted (rejected -> CANCELLED, never executed).

Lifecycle: start() launches the poll loop; stop() halts polling and
waits for in-flight tasks to finish (bounded by shutdown_timeout).
Tasks never claimed stay PENDING. A crashing worker marks its task
FAILED — tasks are never silently dropped or double-executed.

Scope: one process. The in-memory store is per-process, so one
dispatcher per process. With PostgresTaskStore the claim is a single
atomic UPDATE, which also keeps two processes from double-claiming.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from core.logging import get_logger
from .models import Task, TaskStatus, utcnow

log = get_logger("orchestrator.dispatcher")


@dataclass
class DispatcherPolicy:
    max_workers: int = 4
    max_per_agent: int = 2
    max_per_business: int = 2
    poll_interval: float = 0.5
    shutdown_timeout: float = 30.0

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if self.max_per_agent < 1:
            raise ValueError("max_per_agent must be >= 1")
        if self.max_per_business < 1:
            raise ValueError("max_per_business must be >= 1")


class Dispatcher:
    """Polls the store and executes PENDING tasks concurrently."""

    def __init__(self, orchestrator, policy: DispatcherPolicy | None = None,
                 approval_gate=None, worker_id: str | None = None) -> None:
        self.orchestrator = orchestrator
        self.policy = policy or DispatcherPolicy()
        self.gate = approval_gate
        self.worker_id = worker_id or f"dispatcher-{id(self) % 10000}"
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        # task_id -> (agent_type, business_id) for in-flight tasks.
        self._inflight: dict[str, tuple[str, str | None]] = {}
        self._slots_lock = threading.Lock()
        self._stats = {"claimed": 0, "completed": 0, "failed": 0}
        self._stats_lock = threading.Lock()
        self._running = False

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "Dispatcher":
        if self._running:
            raise RuntimeError("dispatcher already started")
        self._stop.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=self.policy.max_workers,
            thread_name_prefix=f"dispatch-{self.worker_id}",
        )
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name=f"dispatcher-poll-{self.worker_id}",
            daemon=True,
        )
        self._running = True
        self._poll_thread.start()
        log.warning("dispatcher started id=%s policy=%s",
                    self.worker_id, self.policy)
        return self

    def stop(self, timeout: float | None = None) -> dict[str, int]:
        """Graceful shutdown: stop polling, finish in-flight tasks.

        Tasks never claimed remain PENDING. Returns final stats.
        """
        timeout = self.policy.shutdown_timeout if timeout is None else timeout
        self._stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=timeout)
        if self._executor is not None:
            # wait=True: in-flight tasks finish; unclaimed PENDING tasks
            # were never submitted, so they stay PENDING.
            self._executor.shutdown(wait=True)
        self._running = False
        stats = self.stats()
        log.warning("dispatcher stopped id=%s stats=%s", self.worker_id, stats)
        return stats

    @property
    def running(self) -> bool:
        return self._running and not self._stop.is_set()

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return dict(self._stats)

    def inflight(self) -> dict[str, tuple[str, str | None]]:
        with self._slots_lock:
            return dict(self._inflight)

    # -- submitting (convenience; delegates to the orchestrator) ---------

    def submit(self, *args, **kwargs) -> Task:
        return self.orchestrator.submit(*args, **kwargs)

    def submit_gated(self, *args, **kwargs):
        return self.orchestrator.submit_gated(*args, **kwargs)

    # -- poll loop --------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._pump()
            except Exception as exc:  # noqa: BLE001 - poll must survive
                log.warning("dispatcher pump error: %s", exc)
            self._stop.wait(self.policy.poll_interval)

    def _pump(self) -> None:
        store = self.orchestrator.store
        if self.gate is not None:
            promote = getattr(store, "promote_approved", None)
            if promote is not None:
                result = promote(self.gate)
                if result["promoted"] or result["cancelled"]:
                    log.info("dispatcher promote: %s", result)
        for task in store.list_tasks(status=TaskStatus.PENDING):
            if self._stop.is_set():
                break
            if not self._reserve_slot(task):
                continue  # no capacity for this agent/business right now
            claimed = store.claim_task(task.id, worker_id=self.worker_id)
            if claimed is None:
                # Lost the race (another worker claimed it): free the slot.
                self._release_slot(task.id)
                continue
            with self._stats_lock:
                self._stats["claimed"] += 1
            assert self._executor is not None
            self._executor.submit(self._execute_claimed, claimed)

    def _reserve_slot(self, task: Task) -> bool:
        """Reserve a concurrency slot; atomic under the slots lock."""
        with self._slots_lock:
            if len(self._inflight) >= self.policy.max_workers:
                return False
            per_agent = sum(1 for a, _ in self._inflight.values()
                            if a == task.agent_type)
            if per_agent >= self.policy.max_per_agent:
                return False
            per_biz = sum(1 for _, b in self._inflight.values()
                          if b == task.business_id)
            if per_biz >= self.policy.max_per_business:
                return False
            self._inflight[task.id] = (task.agent_type, task.business_id)
            return True

    def _release_slot(self, task_id: str) -> None:
        with self._slots_lock:
            self._inflight.pop(task_id, None)

    def _execute_claimed(self, task: Task) -> None:
        """Run one claimed task to completion on a worker thread."""
        try:
            run = self.orchestrator._run_task(task)
            with self._stats_lock:
                if run.status == TaskStatus.COMPLETED:
                    self._stats["completed"] += 1
                else:
                    self._stats["failed"] += 1
        except Exception as exc:  # noqa: BLE001 - never silently drop
            # _run_task already converts handler/verification failures into
            # FAILED runs; this guards against crashes outside that path
            # (e.g. the store itself failing mid-run).
            log.warning("dispatcher worker crash id=%s error=%s", task.id, exc)
            try:
                task.status = TaskStatus.FAILED
                task.error = f"dispatcher worker crash: {exc}"
                task.finished_at = utcnow()
                self.orchestrator.store.save_task(task)
            except Exception:  # noqa: BLE001
                log.warning("dispatcher could not persist crash for %s", task.id)
            with self._stats_lock:
                self._stats["failed"] += 1
        finally:
            self._release_slot(task.id)

    # -- convenience ------------------------------------------------------

    def drain(self, timeout: float = 60.0) -> dict[str, int]:
        """Block until no PENDING or in-flight tasks remain (or timeout)."""
        deadline = time.monotonic() + timeout
        store = self.orchestrator.store
        while time.monotonic() < deadline:
            pending = store.list_tasks(status=TaskStatus.PENDING)
            if not pending and not self.inflight():
                break
            time.sleep(0.05)
        return self.stats()
