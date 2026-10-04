"""Tests for the concurrent dispatcher.

All tests are deterministic: synchronization uses threading Events and
Barriers with generous timeouts (timeouts only trigger on failure, never
on success), and drain() replaces blind sleeps.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agents.base import BaseAgent
from orchestrator.approvals import ApprovalGate
from orchestrator.dispatcher import Dispatcher, DispatcherPolicy
from orchestrator.engine import Orchestrator
from orchestrator.models import Task, TaskStatus
from orchestrator.registry import AgentRegistry


def _agent_class(name, fn):
    return type(
        f"Test{name.title()}Agent",
        (BaseAgent,),
        {"agent_type": name, "run": lambda self, task: fn(self, task)},
    )


def _make_orch(names, fns=None):
    reg = AgentRegistry()
    orch = Orchestrator(registry=reg)
    for name in names:
        reg.register(name, capabilities=[name], description=name)
        fn = (fns or {}).get(name, lambda self, task: {"ok": True})
        orch.register_handler(name, _agent_class(name, fn)())
    return orch


def _wait_until(predicate, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# -- 1. parallel execution across agents and businesses --------------------


def test_concurrent_tasks_across_businesses_and_agents_complete():
    seen_threads = set()
    seen_lock = threading.Lock()
    # Two tasks must actually overlap: the barrier only passes when two
    # worker threads are inside the handler at the same time.
    barrier = threading.Barrier(2, timeout=10)

    def fn(self, task):
        with seen_lock:
            seen_threads.add(threading.get_ident())
        if task.inputs.get("rendezvous"):
            barrier.wait(timeout=10)
        return {"ok": True, "agent": task.agent_type}

    orch = _make_orch(["alpha", "beta"], {"alpha": fn, "beta": fn})
    tasks = [
        orch.submit("alpha", business_id="biz_etsy",
                    inputs={"rendezvous": True}, budget_tokens=1000),
        orch.submit("beta", business_id="biz_cissp",
                    inputs={"rendezvous": True}, budget_tokens=1000),
        orch.submit("alpha", business_id="biz_cissp", budget_tokens=1000),
        orch.submit("beta", business_id="biz_etsy", budget_tokens=1000),
    ]
    disp = Dispatcher(orch, DispatcherPolicy(
        max_workers=4, max_per_agent=2, max_per_business=2,
        poll_interval=0.02)).start()
    try:
        stats = disp.drain(timeout=20)
    finally:
        disp.stop()
    assert stats["completed"] == 4
    assert stats["failed"] == 0
    for t in tasks:
        assert orch.store.get_task(t.id).status == TaskStatus.COMPLETED
    # The barrier passing proves two tasks truly overlapped in time.
    assert len(seen_threads) >= 2


# -- 2. atomic claim: racing dispatchers never double-execute -------------


def test_no_double_execution_under_racing_dispatchers():
    executions: dict[str, int] = {}
    lock = threading.Lock()

    def fn(self, task):
        with lock:
            executions[task.id] = executions.get(task.id, 0) + 1
        return {"ok": True}

    orch = _make_orch(["alpha"], {"alpha": fn})
    tasks = [orch.submit("alpha", business_id="biz_1") for _ in range(20)]
    policy = DispatcherPolicy(max_workers=2, max_per_agent=4,
                              max_per_business=10, poll_interval=0.01)
    d1 = Dispatcher(orch, policy, worker_id="race-1").start()
    d2 = Dispatcher(orch, policy, worker_id="race-2").start()
    try:
        d1.drain(timeout=20)
        d2.drain(timeout=20)
    finally:
        d1.stop()
        d2.stop()
    assert len(executions) == 20
    assert all(n == 1 for n in executions.values()), \
        f"double execution detected: {executions}"
    assert all(orch.store.get_task(t.id).status == TaskStatus.COMPLETED
               for t in tasks)


# -- 3. per-agent concurrency cap ------------------------------------------


def test_per_agent_cap_respected():
    active = 0
    max_active = 0
    lock = threading.Lock()
    release = threading.Event()

    def fn(self, task):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            assert release.wait(timeout=15), "handler released too late"
        finally:
            with lock:
                active -= 1
        return {"ok": True}

    orch = _make_orch(["alpha"], {"alpha": fn})
    tasks = [orch.submit("alpha", business_id="biz_1") for _ in range(4)]
    disp = Dispatcher(orch, DispatcherPolicy(
        max_workers=4, max_per_agent=2, max_per_business=10,
        poll_interval=0.02)).start()
    try:
        # Both cap slots fill...
        assert _wait_until(
            lambda: len(disp.inflight()) == 2, timeout=10), "cap slots unfilled"
        # ...while the other two tasks wait unclaimed.
        time.sleep(0.2)  # let extra pump cycles run
        pending = orch.store.list_tasks(status=TaskStatus.PENDING)
        assert len(pending) == 2, f"cap violated: {len(pending)} pending"
        assert len(disp.inflight()) == 2
        release.set()
        disp.drain(timeout=20)
    finally:
        disp.stop()
    assert max_active == 2, f"cap violated: max_active={max_active}"
    assert all(orch.store.get_task(t.id).status == TaskStatus.COMPLETED
               for t in tasks)


# -- 4. exact per-run budget accounting under concurrency ------------------


class MeteredAgent(BaseAgent):
    agent_type = "metered"

    def run(self, task: Task) -> dict:
        i = task.inputs["i"]
        self.record_usage(task, tokens=100 + i, cost_usd=0.01 * (i + 1))
        self.record_usage(task, tokens=10, cost_usd=0.001)
        return {"i": i}


def test_budget_totals_exact_under_concurrency():
    reg = AgentRegistry()
    reg.register("metered", capabilities=["metered"], description="metered")
    orch = Orchestrator(registry=reg)
    orch.register_handler("metered", MeteredAgent())
    tasks = [orch.submit("metered", business_id=f"biz_{i % 2}",
                         inputs={"i": i}, budget_tokens=100000,
                         budget_usd=100.0)
             for i in range(10)]
    disp = Dispatcher(orch, DispatcherPolicy(
        max_workers=4, max_per_agent=4, max_per_business=10,
        poll_interval=0.02)).start()
    try:
        stats = disp.drain(timeout=20)
    finally:
        disp.stop()
    assert stats["completed"] == 10
    runs = [r for r in orch.store.runs.values()
            if r.task_id in {t.id for t in tasks}]
    assert len(runs) == 10
    expected_tokens = sum(110 + i for i in range(10))
    expected_cost = sum(0.01 * (i + 1) + 0.001 for i in range(10))
    assert sum(r.tokens_used for r in runs) == expected_tokens
    assert abs(sum(r.cost_usd for r in runs) - expected_cost) < 1e-9
    # No cross-talk: each run carries exactly its own task's usage.
    by_task = {r.task_id: r for r in runs}
    for t in tasks:
        i = t.inputs["i"]
        assert by_task[t.id].tokens_used == 110 + i
        assert abs(by_task[t.id].cost_usd - (0.01 * (i + 1) + 0.001)) < 1e-9


# -- 5. approval gates hold under concurrency ------------------------------


def test_approval_gated_task_waits_for_approval():
    executed: list[str] = []
    lock = threading.Lock()

    def fn(self, task):
        with lock:
            executed.append(task.id)
        return {"spent": True}

    gate = ApprovalGate()
    orch = _make_orch(["alpha"], {"alpha": fn})
    disp = Dispatcher(orch, DispatcherPolicy(poll_interval=0.02),
                      approval_gate=gate).start()
    try:
        task, approval = orch.submit_gated(
            "alpha", action="ad_spend_increase", amount_usd=100.0, gate=gate,
            business_id="biz_etsy", inputs={"campaign": "x"},
            budget_usd=5.0, budget_tokens=1000)
        assert approval is not None
        assert orch.store.get_task(task.id).status == TaskStatus.WAITING_APPROVAL
        # Workers are racing, but the gated task must not execute.
        time.sleep(0.5)
        assert executed == []
        assert orch.store.get_task(task.id).status == TaskStatus.WAITING_APPROVAL

        gate.decide(approval.id, approved=True, decided_by="tester")
        assert _wait_until(
            lambda: orch.store.get_task(task.id).status == TaskStatus.COMPLETED,
            timeout=10)
        assert executed == [task.id]

        # A denied approval cancels the task: it never executes.
        task2, approval2 = orch.submit_gated(
            "alpha", action="ad_spend_increase", amount_usd=500.0, gate=gate,
            business_id="biz_etsy", budget_usd=5.0, budget_tokens=1000)
        gate.decide(approval2.id, approved=False, decided_by="tester")
        assert _wait_until(
            lambda: orch.store.get_task(task2.id).status == TaskStatus.CANCELLED,
            timeout=10)
        time.sleep(0.3)
        assert task2.id not in executed
    finally:
        disp.stop()


def test_submit_gated_without_approval_requirement_runs_normally():
    gate = ApprovalGate()
    orch = _make_orch(["alpha"])
    task, approval = orch.submit_gated(
        "alpha", action="ad_spend_increase", amount_usd=10.0, gate=gate,
        business_id="biz_1")
    assert approval is None
    assert task.status == TaskStatus.PENDING
    run = orch.dispatch(task.id)
    assert run.status == TaskStatus.COMPLETED


# -- 6. graceful shutdown ----------------------------------------------------


def test_graceful_shutdown_leaves_pending_tasks_pending():
    release = threading.Event()

    def fn(self, task):
        assert release.wait(timeout=15)
        return {"ok": True}

    orch = _make_orch(["alpha"], {"alpha": fn})
    tasks = [orch.submit("alpha", business_id="biz_1") for _ in range(3)]
    disp = Dispatcher(orch, DispatcherPolicy(
        max_workers=1, max_per_agent=1, max_per_business=1,
        poll_interval=0.02)).start()
    try:
        assert _wait_until(
            lambda: orch.store.get_task(tasks[0].id).status == TaskStatus.RUNNING,
            timeout=10)
        # Release the in-flight task just as shutdown begins: it must
        # finish, while the never-claimed tasks stay PENDING.
        timer = threading.Timer(0.3, release.set)
        timer.start()
        stats = disp.stop()
    finally:
        release.set()
    assert stats["completed"] == 1
    assert orch.store.get_task(tasks[0].id).status == TaskStatus.COMPLETED
    assert orch.store.get_task(tasks[1].id).status == TaskStatus.PENDING
    assert orch.store.get_task(tasks[2].id).status == TaskStatus.PENDING


# -- 7. worker crash marks the task failed, never dropped --------------------


def test_worker_crash_marks_task_failed_not_dropped():
    def fn(self, task):
        if task.inputs.get("fail"):
            raise RuntimeError("boom")
        return {"ok": True}

    orch = _make_orch(["alpha"], {"alpha": fn})
    bad = orch.submit("alpha", business_id="biz_1", inputs={"fail": True})
    good = [orch.submit("alpha", business_id="biz_1") for _ in range(2)]
    disp = Dispatcher(orch, DispatcherPolicy(
        max_workers=2, poll_interval=0.02)).start()
    try:
        stats = disp.drain(timeout=20)
    finally:
        disp.stop()
    assert stats["failed"] == 1
    assert stats["completed"] == 2
    bad_task = orch.store.get_task(bad.id)
    assert bad_task.status == TaskStatus.FAILED
    assert "boom" in (bad_task.error or "")
    assert all(orch.store.get_task(t.id).status == TaskStatus.COMPLETED
               for t in good)
    # The failed run was persisted, not swallowed.
    assert any(r.task_id == bad.id and r.status == TaskStatus.FAILED
               for r in orch.store.runs.values())
