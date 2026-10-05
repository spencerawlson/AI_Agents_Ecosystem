"""Reports + approvals in the dashboard GUI: listing, safe rendering,
mission -> approval wiring, and the approve/reject -> dispatcher loop."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.app import create_app
from dashboard.reports import list_reports, render_markdown, render_report
from ecosystem.runtime import build_runtime, create_dashboard_app
from orchestrator.approvals import ApprovalGate
from orchestrator.dispatcher import Dispatcher, DispatcherPolicy
from orchestrator.models import ApprovalStatus, TaskStatus

MISSION_MD = """# Marketing Mission — https://road2cissp.com

_Generated 2026-10-04T19:58:45.692074+00:00 · niche: CISSP exam preparation_

> **NOTHING WAS PUBLISHED — all drafts below await human approval.** No ads were bought, no emails sent, no posts published.

## Spend

Total AI spend this mission: **$0.0113**
(3846 tokens across 4 agent steps)

## 1. SEO keyword map (live Google Trends, US, 12-month)

| Keyword | Avg interest | Trend |
|---------|--------------|-------|
| cissp practice questions | n/a | unavailable |
| cissp exam prep | 42 | rising |

## 2. Positioning brief

**Audiences**
- **Experienced security professionals** — Need to turn years of hands-on experience into confident performance.
- {'name': 'Career changers', 'pain_point': 'Steep learning curve.'}

### x — Slow down to speed up your CISSP practice.

Slow down to speed up your CISSP practice. Read the full scenario first.

---
"""


# -- markdown rendering -------------------------------------------------


def test_render_markdown_covers_mission_output():
    out = render_markdown(MISSION_MD)
    assert "<h1>Marketing Mission — https://road2cissp.com</h1>" in out
    assert "<em>Generated 2026-10-04T19:58:45.692074+00:00" in out
    assert ("<blockquote><strong>NOTHING WAS PUBLISHED — all drafts below "
            "await human approval.</strong>") in out
    assert "<h2>Spend</h2>" in out
    assert "Total AI spend this mission: <strong>$0.0113</strong>" in out
    assert "<table>" in out and "<th>Keyword</th>" in out
    assert "<td>unavailable</td>" in out and "<td>42</td>" in out
    assert ("<li><strong>Experienced security professionals</strong> — "
            "Need to turn years" in out)
    assert "<h3>x — Slow down to speed up your CISSP practice.</h3>" in out
    assert "<hr>" in out


def test_render_markdown_escapes_everything_else():
    out = render_markdown(
        "# Hi\n\n<script>alert(1)</script>\n\n"
        "[evil](javascript:alert(2))\n\n"
        "> quote with <b>html</b>\n"
    )
    assert "<script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "&lt;b&gt;html&lt;/b&gt;" in out
    # Unknown constructs stay inert text, never links.
    assert "<a " not in out


# -- report listing ------------------------------------------------------


def _write_report(directory: Path, name: str, body: str, mtime: float) -> None:
    p = directory / name
    p.write_text(body, encoding="utf-8")
    os.utime(p, (mtime, mtime))


def test_list_reports_parses_metadata_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))
    _write_report(tmp_path, "marketing_mission_20261003.md",
                  "# Old Mission\n\n_Total AI spend this mission: **$0.0200**_\n",
                  mtime=1000.0)
    _write_report(tmp_path, "marketing_mission_20261004.md", MISSION_MD,
                  mtime=2000.0)
    (tmp_path / "notes.txt").write_text("ignored")
    items = list_reports()
    assert [r["name"] for r in items] == [
        "marketing_mission_20261004.md", "marketing_mission_20261003.md"]
    newest = items[0]
    assert newest["title"] == "Marketing Mission — https://road2cissp.com"
    assert newest["generated_at"] == "2026-10-04T19:58:45.692074+00:00"
    assert newest["spend_usd"] == pytest.approx(0.0113)
    assert newest["date"] == "2026-10-04"
    assert newest["mission"] == "Marketing Mission"


def test_list_reports_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "missing"))
    assert list_reports() == []


def test_render_report_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))
    assert render_report("../secret.md") is None
    assert render_report("..%2fsecret.md") is None
    assert render_report("sub/dir.md") is None
    assert render_report("nope.md") is None


def test_reports_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))
    _write_report(tmp_path, "marketing_mission_20261004.md", MISSION_MD,
                  mtime=2000.0)
    app = create_app()
    client = TestClient(app)
    r = client.get("/reports")
    assert r.status_code == 200
    assert "Marketing Mission — https://road2cissp.com" in r.text
    assert "0.0113" in r.text
    r = client.get("/reports/marketing_mission_20261004.md")
    assert r.status_code == 200
    assert "NOTHING WAS PUBLISHED" in r.text
    assert "<table>" in r.text
    r = client.get("/reports/../secret.md")
    assert r.status_code == 404
    # Portfolio index links the new pages.
    r = client.get("/")
    assert "/reports" in r.text and "/approvals" in r.text


# -- gate persistence across processes ------------------------------------


def test_gate_persistence_across_processes(tmp_path):
    path = tmp_path / "approvals.json"
    g1 = ApprovalGate(persist_path=path)
    a = g1.request("move_funds", amount_usd=5.0, business_id="biz_1")
    assert path.exists()
    # A second gate (another process) sees the pending approval.
    g2 = ApprovalGate(persist_path=path)
    assert [x.id for x in g2.pending()] == [a.id]
    # Deciding in the second process is visible in the first.
    g2.decide(a.id, approved=True, decided_by="owner")
    assert g1.pending() == []
    assert g1.get(a.id).status == ApprovalStatus.APPROVED
    # Concurrent requests from two processes merge — none lost.
    g3 = ApprovalGate(persist_path=path)
    b = g3.request("sign_contract", business_id="biz_1")
    c = g1.request("legal_response", business_id="biz_1")
    assert {x.id for x in g1.pending()} == {b.id, c.id}
    g4 = ApprovalGate(persist_path=path)
    assert {x.id for x in g4.pending()} == {b.id, c.id}


def test_gate_stays_in_memory_without_path():
    g = ApprovalGate()
    g.request("move_funds", amount_usd=5.0)
    assert len(g.pending()) == 1
    assert g._persist_path is None


# -- mission -> approval wiring --------------------------------------------


def test_mission_completion_requests_approval(tmp_path):
    from ecosystem.marketing_mission import request_report_approval

    gate = ApprovalGate()
    rt = SimpleNamespace(approvals=gate)
    biz = SimpleNamespace(id="biz_9", name="Road to CISSP")
    rp = tmp_path / "marketing_mission_20261004.md"
    rp.write_text("# report")
    ctx = {"site_url": "https://road2cissp.com",
           "drafts": [{"hook": "h"}] * 10,
           "total_spend_usd": 0.0113, "total_tokens": 3846,
           "steps_completed": 4}
    appr = request_report_approval(rt, biz, rp, ctx)
    assert appr.action == "mission_report_approval"
    assert appr.business_id == "biz_9"
    assert appr.details["report_name"] == "marketing_mission_20261004.md"
    assert appr.details["drafts_count"] == 10
    assert appr.details["business_name"] == "Road to CISSP"
    assert appr.details["total_spend_usd"] == pytest.approx(0.0113)
    assert len(gate.pending()) == 1


# -- full approve/reject loop ----------------------------------------------


class _StubAgent:
    """Deterministic handler: records executions, always verifies clean."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, task):
        self.calls.append(task.id)
        return {"ok": True, "task_id": task.id}


def _loop_fixture():
    rt = build_runtime()
    app = create_dashboard_app(rt)
    # The dashboard GUI and the dispatcher must share one gate object.
    assert app.state.ecosystem["approvals"] is rt.approvals
    rt.agent_registry.register("stub_agent", capabilities=["test"],
                               description="stub")
    stub = _StubAgent()
    rt.orchestrator.register_handler("stub_agent", stub)
    biz = rt.businesses.create("Loop Biz", "test")
    dispatcher = Dispatcher(
        rt.orchestrator,
        policy=DispatcherPolicy(max_workers=2, poll_interval=0.05),
        approval_gate=rt.approvals,
    )
    assert dispatcher.gate is rt.approvals
    return rt, app, stub, biz, dispatcher


def _submit(rt, biz):
    return rt.orchestrator.submit_gated(
        "stub_agent", action="move_funds", amount_usd=10.0,
        gate=rt.approvals, business_id=biz.id,
        inputs={"action": "noop"}, budget_usd=1.0, budget_tokens=100)


def test_approve_promotes_and_executes():
    rt, app, stub, biz, dispatcher = _loop_fixture()
    client = TestClient(app)
    task, appr = _submit(rt, biz)
    assert task.status == TaskStatus.WAITING_APPROVAL

    r = client.get("/approvals")
    assert r.status_code == 200
    assert "move_funds" in r.text and "Loop Biz" in r.text

    r = client.post(f"/approvals/{appr.id}/decide", data={"approved": "true"})
    assert r.status_code == 200

    dispatcher.start()
    try:
        dispatcher._pump()
        dispatcher.drain(timeout=10)
    finally:
        dispatcher.stop()

    assert stub.calls == [task.id]
    done = rt.orchestrator.store.get_task(task.id)
    assert done.status == TaskStatus.COMPLETED


def test_reject_cancels_and_never_executes():
    rt, app, stub, biz, dispatcher = _loop_fixture()
    client = TestClient(app)
    task, appr = _submit(rt, biz)

    r = client.post(f"/approvals/{appr.id}/decide", data={"approved": "false"})
    assert r.status_code == 200

    dispatcher.start()
    try:
        dispatcher._pump()
        dispatcher.drain(timeout=10)
    finally:
        dispatcher.stop()

    assert stub.calls == []
    done = rt.orchestrator.store.get_task(task.id)
    assert done.status == TaskStatus.CANCELLED


def test_approvals_page_human_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))
    _write_report(tmp_path, "marketing_mission_20261004.md", MISSION_MD,
                  mtime=2000.0)
    rt = build_runtime()
    app = create_dashboard_app(rt)
    client = TestClient(app)
    rt.approvals.request(
        action="mission_report_approval",
        details={"mission": "marketing_mission",
                 "business_name": "Road to CISSP",
                 "site_url": "https://road2cissp.com",
                 "report_name": "marketing_mission_20261004.md",
                 "drafts_count": 10, "total_spend_usd": 0.0113},
        amount_usd=0.0113, business_id="biz_other_process",
        requested_by="marketing_mission")
    r = client.get("/approvals")
    assert r.status_code == 200
    # Human-readable: business name, drafts, spend, report link — no raw ids.
    assert "Road to CISSP" in r.text
    assert "10 drafts" in r.text
    assert "0.0113" in r.text
    assert "/reports/marketing_mission_20261004.md" in r.text
    assert "biz_other_process" not in r.text
    assert "nothing is published automatically" in r.text.lower()
    # Deciding twice is a friendly 400, not a 500.
    appr = rt.approvals.pending()[0]
    client.post(f"/approvals/{appr.id}/decide", data={"approved": "true"})
    r = client.post(f"/approvals/{appr.id}/decide", data={"approved": "true"})
    assert r.status_code == 400
