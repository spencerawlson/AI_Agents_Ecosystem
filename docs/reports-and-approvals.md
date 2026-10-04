# Reports + Approvals in the GUI

Mission reports (`reports/*.md`) are viewable at **`/reports`**, and
human sign-off happens at **`/approvals`**. Both are linked from the
portfolio index page.

## The flow

1. A mission script (today: `ecosystem/marketing_mission.py`) runs its
   agent steps, writes `reports/marketing_mission_<date>.md`, then calls
   `request_report_approval(rt, biz, report_path, ctx)`.
2. That registers an approval with `action="mission_report_approval"` and
   details: mission name, business name, site URL, report path, drafts
   count, total AI spend. The business name is baked into the approval
   because mission-created businesses don't exist in the dashboard's
   registry (different process).
3. **`/approvals`** shows pending requests with human summaries — what it
   is, which business, spend, when requested — plus a **View report** link
   for mission approvals. Pending first with Approve/Reject, then recently
   decided.
4. **`/reports`** lists reports newest-first (title, mission, date, parsed
   AI spend) and renders each to safe HTML. There is no `markdown`
   dependency: `dashboard/reports.py` renders exactly what missions emit
   (`#`/`##`/`###`, `>` quotes, `**bold**`, `_italic_`, `` `code` ``,
   `|` tables `|`, `-` lists, `---` rules) and escapes everything else,
   so a report can never inject markup or scripts.

## Banner semantics (do not break)

Approving a mission report means **"a human signed off on the drafts"**.
It never publishes anything on its own — no ads bought, no emails sent,
no posts published. Publishing stays a separate, explicit human action.
The `NOTHING WAS PUBLISHED` banner in the report and the disclaimer on
`/approvals` both say this.

## Gate wiring across launch modes

`ApprovalGate` is thread-safe in-process. For cross-process visibility
(the mission script exits; the dashboard is a separate process), the gate
is file-backed: `build_runtime(approvals_persist=True)` persists to
`data/approvals.json` (`APPROVALS_PATH` env overrides).

- `launch.py dashboard` and `launch.py worker` each build their own
  runtime, but both use the persisted file, so approvals filed by one are
  visible in the other.
- `launch.py all` builds **one shared runtime** and hands it to both the
  worker thread and the dashboard — the GUI queue and the dispatcher's
  gate are literally the same object (`app.state.ecosystem["approvals"]
  is rt.approvals` is asserted in tests).

Sync protocol: every `request()`/`decide()` saves synchronously;
`pending()`/`get()`/`decide()` refresh when the file's revision counter
moved. Concurrent writers merge by approval id.

## Honest limitations

- `POST /api/reset` rebuilds the runtime, but a dispatcher thread started
  by `launch.py all` keeps the *old* runtime's references. After a reset,
  restart the worker (or `all`) so the dispatcher uses the new gate.
- File merge is last-writer-wins per approval id; on conflict the
  in-memory copy wins because it always saves before returning.
- The dashboard and a *separately launched* worker only share approvals
  through the file (polling on each `pending()` call), not live objects.
