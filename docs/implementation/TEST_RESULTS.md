# Test results — Ventoryn first-customer program, Phase 1

Phase 1 (repair only): gate `/api/analyze`, fix the two deployment
leftovers. No OAuth, no publishing code, no social connections, no
live spend, no deploy, no pricing/credit changes, no push.

## Full suite

Command (from the build copy, proxy vars unset):

```
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u NO_PROXY -u no_proxy \
  ~/workspace/.venvs/ecosystem/bin/python \
  -m pytest -q -p no:cacheprovider
```

**Result: 603 passed, 0 failed, 1 warning in 51.28s.**

Baseline at `4a697dd5` was 592 passed. The +11 are the new tests in
`tests/test_analyze_preview.py` (below). The one warning is the
pre-existing Starlette `httpx` deprecation in `fastapi/testclient.py`
— not from this change.

### One failure found and fixed during the run

`tests/test_countries.py::test_analyze_accepts_a_spread_of_codes_end_to_end`
failed on the first full run: it starts six anonymous previews from
one TestClient (one IP), and the 6th hit the new 5/hour limit (429).
The test is about country-code validation, not anonymity, so it now
signs in — signed-in previews exercise the same validation path
without the limit. Full suite re-run after the fix: 603 passed.

## New tests — `tests/test_analyze_preview.py` (11)

- `test_anonymous_preview_is_summary_only` — scores + findings, no
  `report_url`, no `report_name`, report fetch 404s.
- `test_anonymous_preview_writes_no_report_file` — the real pipeline,
  run as an anonymous preview, writes no file under `reports/`.
- `test_signed_in_preview_is_summary_only` — a signed-in login does
  not buy the full report either; the paid job is the only route.
- `test_unverified_session_cannot_use_preview` — a signed-in session
  that has not finished 2FA gets a 303 to `/account/two-factor/setup`
  (the `/api/analyze` carve-out is gone from the gate).
- `test_anonymous_preview_rate_limit` — 5 anonymous previews start
  (202); the 6th is refused with 429, a plain message, and a
  `Retry-After` header.
- `test_signed_in_preview_not_rate_limited` — six signed-in previews
  in a row all start (the limit is anonymous-only).
- `test_rejected_preview_attempts_do_not_burn_quota` — five 400s
  (bad URLs) do not count; the next valid preview still starts.
- `test_preview_client_ip_prefers_cf_connecting_ip` /
  `test_preview_client_ip_falls_back_to_peer` — IP derivation, and
  that `X-Forwarded-For` is deliberately not trusted.
- `test_preview_allowed_allows_five_then_denies` /
  `test_preview_window_prunes_stale_hits` — limiter units.

## Updated tests

- `tests/test_analyze_box.py` — `test_analyze_job_lifecycle`,
  `test_analyze_real_pipeline_writes_report` (renamed to
  `test_analyze_real_pipeline_writes_no_report`), and
  `test_analyze_publishes_nothing` now pin the preview behaviour
  (no report file, no `report_url`, report fetch 404s). Updated, not
  deleted: each keeps the behaviour it pinned or states why it
  changed.
- `tests/test_countries.py::test_analyze_accepts_a_spread_of_codes_end_to_end`
  — now signs in (see above).

## Unchanged coverage for the paid path

`tests/test_saas_shell.py::test_job_analyze_charges_and_reports`
still pins the paid job: 5 credits charged, report file written with
both scores. It was not modified.

## Served checks — 21/21 passed

Live server on `127.0.0.1:8099` from the build copy, seeded
file-backed state (operator + customer accounts with TOTP-verified
sessions, customer with 100 credits and one site), real pipeline
(no injected runner). Seeded state and scratch scripts were deleted
afterwards.

| # | Check | Result |
|---|---|---|
| 1 | Anonymous preview POST is 202 | PASS |
| 2 | Anonymous preview finishes `done` | PASS |
| 3 | Anonymous preview has no `report_url` | PASS |
| 4 | Anonymous preview result has no `report_name` | PASS |
| 5 | Anonymous preview has scores | PASS |
| 6 | Anonymous report fetch is 404 | PASS |
| 7 | Anonymous previews 2–5 start (202) | PASS |
| 8 | 6th anonymous preview is 429 | PASS |
| 9 | 429 names the free preview limit | PASS |
| 10 | 429 carries `Retry-After` (observed 3596s) | PASS |
| 11 | Signed-in preview POST is 202 | PASS |
| 12 | Signed-in preview has no `report_url` | PASS |
| 13 | Signed-in preview report fetch is 404 | PASS |
| 14 | Paid job POST redirects (303) | PASS |
| 15 | Paid job wrote a report file (`analyze_example-com_2026-11-09.md`) | PASS |
| 16 | Paid report has a site score | PASS |
| 17 | `/ops` is 200 | PASS |
| 18 | `/ops` has the new panel copy | PASS |
| 19 | `/ops` has no "Full report" link | PASS |
| 20 | Landing is 200 | PASS |
| 21 | Landing still says "free preview" | PASS |

Notes from the served run:

- The paid job's report is written by `saas_routes.py` to the
  repo's `reports/` directory (`REPORTS_DIR` module constant), not
  to the `REPORTS_DIR` environment path that
  `ventoryn/reports.py::reports_dir()` reads — the two paths agree
  in production (no env override), but the served check first
  looked in the wrong one and was corrected.
- The sandbox fetch of `example.com` scores low (13/100 site,
  14/100 GEO) — honest pipeline output through the sandbox proxy,
  not a test artifact.

## Rate limit chosen

**5 anonymous previews per client IP per rolling hour**
(`PREVIEW_PER_IP_PER_HOUR` in `ventoryn/analyze_jobs.py`).
In-memory, in the job store; a restart resets it. Client IP is
`CF-Connecting-IP` when present (the production ingress is the
Cloudflare tunnel, which overwrites it), else the TCP peer.
`X-Forwarded-For` is deliberately not trusted. Signed-in previews
are not limited — their usage is attributed to an account.

## Design decisions (for the record)

- **Summary-only for everyone via `/api/analyze`, not just
  anonymous users.** The task's rule (c) is explicit — the full
  report comes only from the paid job — and sign-up is open, so a
  "signed-in users still get it free" rule would leave the hole
  open behind one Google sign-in. The operator is not exempt
  either: the paid job is free for the operator (unlimited
  credits, charged 0).
- **`GET /api/analyze/{job_id}/report` is kept, not deleted.**
  It 404s whenever there is no report, which is now always for
  preview jobs; the strip in `_run_job` plus the tests pin that
  invariant. Removing a public route was a bigger surface change
  than keeping a correctly-404ing one.
- **Compose mount dropped, not replaced with an init dir.** The
  schema is created by `launch.py initdb`
  (`python -m infra.init_db`, SQLAlchemy `create_all`) — nothing
  in the repo needs Postgres init scripts, so an empty init dir
  would have been misleading.
- **Service paths corrected to `~/Projects/AI_Agents_Ecosystem`**
  with a header note that the live deploy runs a manual
  `python launch.py all`, not this unit. No evidence the unit is
  used for the live deploy was found.
- **Landing copy unchanged.** It already promised only "your site
  score, your answer-engine (GEO) score, and the top things to
  fix" — true under the new behaviour. The /ops panel copy was
  updated ("the report is saved locally and shown here" was
  false).

## Deliberately left out

Everything outside the Phase 1 scope: no OAuth builds, no
publishing code, no social connections, no live spend, no deploy,
no pricing/credit changes, no push. The `docker-compose.yml` and
`ecosystem.service` dev-default Postgres password
(`ecosystem`/`ecosystem`) was noticed and left untouched — out of
scope, flag separately if it should change.

## Rearrangement (whole-site, approved 2026-11-08) — 2026-11-09

Stacked on Phase 1 in this same copy. No route moves or renames;
only labels, titles, and section order changed.

- **Sidebar (customer):** Overview → Websites → Campaigns →
  Approvals → Agents → Analytics (absorbs Attribution; /report
  stays as a deep link) → Customers → Integrations → My account
  (was Settings). Admin group: Live, Subscribers, Accounts,
  Agent activity (was Agents). Overview header title: "Live Ops"
  → "Overview" (back links, error pages, and the admin "Live
  now" labels renamed with it).
- **Overview order:** progress ring + stats → "Needs your
  decision" strip (top pending approval inline, Approve/Reject
  through the SAME POST /approvals/{id}/decide endpoint, with a
  same-site `next` return; quiet line when nothing is pending)
  → Your websites (cards gain verified-customers + View link;
  analyze box first when the account has zero sites) → Analyze
  a site (its one home — removed from Portfolio and Businesses)
  → merged Results (flow stepper, donut + campaign table in one
  card, trend; duplicate "Conversion breakdown" card gone) →
  Live activity (latest 8 + "View all on Agents") → quiet
  Data & privacy link. Pause: slim pill in the header row
  (operator only; "Agents running — Pause" / "Agents paused —
  Resume"), full card stays in My account.
- **Page-by-page:** Websites (/portfolio) — stats + website cards,
  Operator businesses second group, no analyze box. Campaigns —
  intro copy. Approvals — rows expandable (summary + full
  payload). Agents — approval-rule line at the top. Analytics —
  donut card + Report section; /report unchanged. Customers —
  signature tag links to its campaign. Integrations — Stripe
  sales unconnected hint per site. My account — sub-nav (My
  account · Security · Subscribe). Website page — funnel →
  Run agent work → Jobs → Paying customers → Connections →
  Data & privacy → How attribution works. Landing, 2FA pages,
  admin pages (except the rename), /game → /ops redirect, and
  shell 404/500 unchanged.

**Tests:** full suite **621 passed, 0 failed** (baseline 603 +
18 new in `tests/test_rearrangement.py`: sidebar order/labels,
/report deep link, Overview order + strip, strip approve/reject
through the real endpoint, cross-account 403, analyze box on
Overview only, My account sub-nav, website page order,
expandable approvals, Analytics donut + report, Customers
signature link, Integrations Stripe hint, Websites operator
group, Agents/Campaigns copy). Existing tests that pinned the
old labels/order were updated, not deleted. 1 pre-existing
Starlette httpx deprecation warning.

**Served checks (live server, seeded file-backed state, port
8099): 60/60 passed** — landing 200; all signed-in pages 200 /
303 unsigned; sidebar labels in the proposed order (customer +
operator); Overview in proposal order with the decision strip;
inline Approve through the real endpoint (303 to
/ops?decided=<id>, banner "Approved — nothing ships without
your say-so.", strip goes quiet, approval lands in Decided);
/game 303 → /ops; /report reachable directly with Analytics
current; analyze box on Overview only; Pause pill in the header
+ full card in My account; My account sub-nav; website page
order; expandable approvals; Analytics donut; Customers
signature link; Integrations Stripe hint. Verify server killed
by exact listener PID; build copy cleaned (data/, reports/,
*.db, pytest-debug.log, __pycache__).

**Deliberately left out:** no push, no deploy, no live actions.
The landing page's own "Live Ops" wording (hero preview title,
"Continue to Live Ops" button) was left unchanged per the
brief. The "Conversion breakdown" card stays on /report.
