# Concurrent dispatcher

The orchestrator's `dispatch()` runs one task at a time. The dispatcher
(`orchestrator/dispatcher.py`) runs tasks **in parallel across agents and
businesses** — e.g. Etsy store operations and Road to CISSP marketing at
the same time — through the exact same execution path: same handler call,
same five-layer verification, same bounded rework loop.

## How it works

- A poll loop finds `PENDING` tasks (oldest first) and **claims them
  atomically** (`PENDING` → `RUNNING` in one locked step). Two workers can
  never execute the same task.
- Claimed tasks run on a thread pool.
- Concurrency policy (all configurable):
  - `max_workers` (default 4) — total threads executing tasks
  - `max_per_agent` (default 2) — concurrent tasks per agent type
  - `max_per_business` (default 2) — concurrent tasks per business
- A task needing human approval is submitted with
  `orchestrator.submit_gated(...)`: it waits in `WAITING_APPROVAL` and is
  promoted to `PENDING` only after the approval is granted. A denied
  approval cancels the task — it never executes.
- `stop()` is graceful: polling halts, in-flight tasks finish, tasks never
  claimed stay `PENDING`. A crashing worker marks its task `FAILED` — work
  is never silently dropped.

## Running it

```bash
cd ~/AI_Agents_Ecosystem
git pull

# Terminal 1: start the dispatcher (tick loop stays the default)
python launch.py worker -- --dispatcher --max-workers 4

# Terminal 2: submit work (same machine, same process store — see below)
python3 - <<'EOF'
from ecosystem.runtime import build_runtime
rt = build_runtime()
etsy = rt.businesses.create("Evergreen Planners", "etsy")
cissp = rt.businesses.create("Road to CISSP", "education_website")
rt.orchestrator.submit("operations", business_id=etsy.id,
                       inputs={"action": "check_health"},
                       budget_usd=1.0, budget_tokens=2000)
rt.orchestrator.submit("marketing", business_id=cissp.id,
                       inputs={"action": "seo_keyword_map",
                               "niche": "CISSP exam preparation",
                               "seeds": ["cissp practice questions"]},
                       budget_usd=1.0, budget_tokens=4000)
print("submitted")
EOF
```

Approval-gated example (stays `WAITING_APPROVAL` until you approve it in
the dashboard's approval queue):

```python
task, approval = rt.orchestrator.submit_gated(
    "marketing", action="ad_spend_increase", amount_usd=100.0,
    gate=rt.approvals, business_id=cissp.id, inputs={...},
    budget_usd=5.0, budget_tokens=8000)
```

## Honest limitations

- **One process.** The default in-memory store is per-process: the
  dispatcher only sees tasks submitted in its own process. Run the
  submitter and the dispatcher in the same process, or switch to
  `PostgresTaskStore` (`--use-postgres`), whose claim is a single atomic
  `UPDATE ... WHERE status='pending'` — safe across processes.
- **Threads, not processes.** CPU-heavy agents share the GIL; the win is
  concurrent I/O (API calls, market data) and parallel businesses, which is
  what these agents do.
- **In-flight tasks on process death** stay `RUNNING` in the store (same
  as the synchronous path before this change). Re-submit or reset them
  manually.
- The marketing mission (`ecosystem/marketing_mission.py`) stays
  sequential on purpose: its steps depend on each other's outputs.

## Thread-safety notes for agent authors

- `BaseAgent` usage counters (`tokens_used` / `cost_usd`) are
  thread-local: one handler instance can serve concurrent tasks and
  per-run deltas stay exact.
- If your agent keeps mutable state across `run()` calls (caches,
  counters, asset stores), guard it with a lock — see `CreativeAgent`'s
  `_id_lock` for the pattern.
