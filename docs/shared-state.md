# Shared state (businesses + ledger)

By default every ecosystem process keeps its businesses and ledger in
its own memory: the dashboard, the worker and the missions each see a
different portfolio, and a restart wipes everything. That is the right
default for tests — and the wrong one for running real businesses.

Set **`ECOSYSTEM_STATE_DB`** to a SQLite file path and
`build_runtime()` swaps in the shared store (`core/state_store.py`):
businesses and ledger entries live in that one file, every process on
the box reads and writes the same books, and the GUI shows real
businesses (e.g. the Farelune business a growth mission creates, with
its real P&L) the moment they exist.

## Turn it on (FedServ)

**Recommended: put it in the repo's `.env` file** (gitignored, never
committed). `build_runtime()` loads `<repo>/.env` for every entry
point — dashboard, worker, missions — filling only variables the
shell hasn't already set, so no terminal needs any exports at all:

```bash
# ~/Projects/AI_Agents_Ecosystem/.env
OPENAI_API_KEY=<your key>
ECOSYSTEM_STATE_DB=/home/srspady/Projects/AI_Agents_Ecosystem/data/ecosystem_state.db
```

Use an absolute path for the database (`.env` values are literal —
`~` is not expanded). The same file is the durable home for the API
key: every process gets the real brain, not just the shell that
happened to export it. (`ECOSYSTEM_ENV_FILE` overrides the file's
location; a real shell export always wins over the file.)

Alternatively, export the variable in every shell that runs ecosystem
code:

```bash
export ECOSYSTEM_STATE_DB=~/Projects/AI_Agents_Ecosystem/data/ecosystem_state.db
```

Either way, restart the dashboard (so it rebuilds its runtime on the
shared store) and run missions as usual — e.g.
`python3 ecosystem/farelune_growth.py` — and the business appears in
the portfolio and the game view.

Notes:

- The tick loop's game counters (tick number, per-agent session stats)
  are still per-process; businesses and money are what's shared.
- The first run against a fresh database starts an empty portfolio.
  Earlier in-memory runs are not migrated (their report files remain
  in `reports/`).
- SQLite WAL mode handles the dashboard + mission concurrency on one
  box. It is a single-host store by design.
