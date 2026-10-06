"""Headless worker: runs the ecosystem's real agent loop on a tick interval.

Focus: the agents' job is marketing road2cissp.com. Every tick runs the
Road to CISSP growth program's due jobs (site audit, keyword research,
metrics, weekly strategy, content drafts — see ecosystem/road2cissp_growth.py).
Cadences live in the program's state file, so frequent ticks are cheap.

Opt-in extras (off by default so the agents stay focused):
    --discovery   opportunity pipeline each tick (discovery → research →
                  scoring → winner becomes a business)
    --store       Shopify store operations (paused project)

Usage:
    python launch.py worker                # run forever, 60s between ticks
    python launch.py worker --ticks 5       # run 5 ticks then exit
    python launch.py worker --interval 300  # 5 minutes between ticks
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.models import BusinessStatus, Opportunity
from core.scoring import ScoringEngine
from ecosystem.runtime import build_runtime
from orchestrator.models import TaskStatus

MONITOR_STATE_PATH = Path.home() / ".config" / "evergreen-etsy" / "monitor_state.json"


def _load_monitor_state() -> dict:
    if MONITOR_STATE_PATH.exists():
        return json.loads(MONITOR_STATE_PATH.read_text())
    return {"last_revenue_usd": 0.0}


def _save_monitor_state(state: dict) -> None:
    MONITOR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MONITOR_STATE_PATH.write_text(json.dumps(state, indent=1))


def run_etsy_monitor_tick(rt, tick: int) -> dict | None:
    """Check Experiment 001 against the charter. Returns the snapshot,
    or None when Etsy isn't configured (warns once). Idempotent: revenue
    is recorded to the ledger only as deltas."""
    from ecosystem.etsy_monitor import (
        EXPERIMENT_BUSINESS_NAME,
        EXPERIMENT_BUSINESS_TYPE,
        EtsyCredentialsError,
        EtsyMonitor,
    )

    try:
        snapshot = EtsyMonitor().check()
    except EtsyCredentialsError as exc:
        if tick == 1 or tick % 50 == 1:
            print(f"etsy monitor: skipped ({exc})", flush=True)
        return None
    except Exception as exc:  # noqa: BLE001 - monitor must not kill worker
        print(f"etsy monitor: ERROR {exc}", flush=True)
        return None

    # Ensure the experiment's business exists in this runtime's registry.
    biz = next((b for b in rt.businesses.list()
                if b.name == EXPERIMENT_BUSINESS_NAME), None)
    if biz is None:
        biz = rt.businesses.create(EXPERIMENT_BUSINESS_NAME,
                                   EXPERIMENT_BUSINESS_TYPE)

    state = _load_monitor_state()
    delta = round(snapshot["revenue_usd"] - state.get("last_revenue_usd", 0.0), 2)
    if delta > 0:
        rt.ledger.record(business_id=biz.id, kind="revenue",
                         amount_usd=delta,
                         description=f"Etsy sales to tick {tick}")
        state["last_revenue_usd"] = snapshot["revenue_usd"]
        _save_monitor_state(state)

    print(
        f"etsy tick {tick}: day {snapshot['days_elapsed']}/60 | "
        f"{snapshot['active_listings']} listings | {snapshot['orders']} orders | "
        f"${snapshot['revenue_usd']:.2f} revenue / ${snapshot['spend_usd']:.2f} spend | "
        f"pace {snapshot['pace_pct']}% | {snapshot['verdict']}",
        flush=True,
    )
    if snapshot["verdict"] == "STOP_LOSS":
        print("!" * 70, flush=True)
        for trigger in snapshot["stop_loss_triggers"]:
            print(f"STOP-LOSS TRIGGERED: {trigger}", flush=True)
        print("!" * 70, flush=True)
        rt.audit.record(agent_type="etsy_monitor", event="stop_loss_triggered",
                        business_id=biz.id, result=snapshot)
    return snapshot


def run_store_tick(rt, tick: int, optimize_every: int = 30) -> dict | None:
    """Shopify store operations for this tick.

    Every tick: publish approved suppliers, launch approved ads, draft
    paused ads for new products. Every `optimize_every` ticks also: spend
    guard, profit, SEO fixes, blog drafts, store report. No-op unless
    Shopify credentials are configured; never raises.
    """
    from core.shopify_adapter import ShopifyCommerceAdapter

    if not ShopifyCommerceAdapter.configured():
        return None
    from core.ads import configured_ads_adapters
    from core.cj_orders import CJOrders
    from core.llm import LLMGateway
    from core.supplier_search import CJDropshippingSource
    from dashboard.reports import reports_dir
    from ecosystem.shopify_pipeline import default_state_path
    from ecosystem.store_ops import run_store_cycle

    optimize = bool(optimize_every) and tick % optimize_every == 0
    try:
        summary = run_store_cycle(
            rt.approvals, ShopifyCommerceAdapter.from_env(),
            configured_ads_adapters(), default_state_path(), reports_dir(),
            gateway=LLMGateway() if (optimize and LLMGateway.enabled()) else None,
            optimize=optimize,
            cj_orders=CJOrders.from_env() if CJDropshippingSource.configured() else None)
    except Exception as exc:  # noqa: BLE001 - worker must survive
        print(f"store: ERROR {exc}", flush=True)
        return None
    for r in summary["published"]:
        print(f"store publish: {r.get('approval_id')} -> {r.get('status')}"
              + (f" ({r['error']})" if r.get("error") else ""), flush=True)
    for r in summary["fulfilment"]:
        print(f"store order: {r}", flush=True)
    for r in summary["ads"] + summary["drafted"]:
        print(f"store ads: {r}", flush=True)
    for g in summary["guard"]:
        print(f"store guard: {g['campaign']} {g['action']} {g.get('reason', '')}",
              flush=True)
    for e in summary["errors"]:
        print(f"store: ERROR {e}", flush=True)
    return summary


def run_growth_tick(rt, tick: int) -> dict | None:
    """Road to CISSP growth program: run whatever jobs are due. Never raises."""
    from ecosystem.road2cissp_growth import run_growth_cycle

    try:
        summary = run_growth_cycle(rt)
    except Exception as exc:  # noqa: BLE001 - worker must survive
        print(f"growth: ERROR {exc}", flush=True)
        return None
    for key in ("events", "ran", "errors"):
        for line in summary[key]:
            print(f"growth {key[:-1] if key != 'ran' else 'job'}: {line}", flush=True)
    if tick == 1:
        for line in summary["skipped"]:
            print(f"growth skipped: {line}", flush=True)
    if summary["report"]:
        print(f"growth report: {summary['report']} "
              f"(spend ${summary['spend_usd']:.4f})", flush=True)
    return summary


def run_tick(rt, tick: int) -> dict:
    orch = rt.orchestrator
    scorer = ScoringEngine()

    t1 = orch.submit("discovery", inputs={"limit": 5},
                     budget_usd=1.0, budget_tokens=5000)
    r1 = orch.dispatch(t1.id)
    if r1.status != TaskStatus.COMPLETED:
        return {"tick": tick, "ok": False, "error": f"discovery: {r1.error}"}
    opportunities = r1.output["opportunities"]

    t2 = orch.submit("research", inputs={"opportunities": opportunities},
                     budget_usd=2.0, budget_tokens=8000)
    r2 = orch.dispatch(t2.id)
    if r2.status != TaskStatus.COMPLETED:
        return {"tick": tick, "ok": False, "error": f"research: {r2.error}"}
    reports = r2.output["reports"]

    pursued = [o for o, rep in zip(opportunities, reports)
               if rep["verdict"] == "pursue"]
    ranked = scorer.score_and_rank([Opportunity(**o) for o in pursued])
    winner = ranked[0] if ranked else None

    ai_spend = r1.cost_usd + r2.cost_usd
    runs = [
        {"agent_type": "discovery", "tokens_used": r1.tokens_used,
         "cost_usd": r1.cost_usd},
        {"agent_type": "research", "tokens_used": r2.tokens_used,
         "cost_usd": r2.cost_usd},
    ]
    business_id = None
    if winner is not None:
        biz = rt.businesses.create(winner.niche, winner.business_type)
        rt.businesses.transition(biz.id, BusinessStatus.RESEARCHING)
        business_id = biz.id
        rt.ledger.record(business_id=biz.id, kind="ai_spend",
                         amount_usd=-ai_spend,
                         description=f"tick {tick} pipeline")
        rt.audit.record(agent_type="worker", event="tick_completed",
                        business_id=biz.id,
                        result={"tick": tick, "winner": winner.niche,
                                "score": winner.score})

    return {
        "tick": tick,
        "ok": True,
        "opportunities": len(opportunities),
        "pursued": len(pursued),
        "winner": winner.niche if winner else None,
        "score": winner.score if winner else None,
        "business_id": business_id,
        "ai_spend_usd": round(ai_spend, 4),
        "runs": runs,
    }


def run_dispatcher(rt, args) -> int:
    """Run the concurrent dispatcher: executes whatever tasks are submitted
    to the orchestrator, across agents and businesses in parallel.

    Submit work from another shell, e.g.:
        python - <<'EOF'
        from ecosystem.runtime import build_runtime
        rt = build_runtime()
        biz_a = rt.businesses.create("Evergreen Planners", "etsy")
        biz_b = rt.businesses.create("Road to CISSP", "education_website")
        rt.orchestrator.submit("operations", business_id=biz_a.id,
                               inputs={"action": "check_health"},
                               budget_usd=1.0, budget_tokens=2000)
        rt.orchestrator.submit("marketing", business_id=biz_b.id,
                               inputs={"action": "seo_keyword_map", "seeds": []},
                               budget_usd=1.0, budget_tokens=4000)
        EOF
    Note: with the default in-memory store the dispatcher only sees tasks
    submitted in this process. Use --use-postgres (separate process submits
    against the same database) for cross-process work queues.
    """
    from orchestrator.dispatcher import Dispatcher, DispatcherPolicy

    policy = DispatcherPolicy(
        max_workers=args.max_workers,
        max_per_agent=args.max_per_agent,
        max_per_business=args.max_per_business,
    )
    dispatcher = Dispatcher(rt.orchestrator, policy=policy,
                            approval_gate=rt.approvals,
                            worker_id="worker-dispatcher")
    dispatcher.start()
    print(f"dispatcher online: {policy} "
          f"store={'postgres' if args.use_postgres else 'memory'} "
          "(Ctrl-C to stop gracefully)", flush=True)
    try:
        while True:
            time.sleep(5)
            stats = dispatcher.stats()
            pending = len(rt.orchestrator.pending_tasks())
            print(f"dispatcher: claimed={stats['claimed']} "
                  f"completed={stats['completed']} failed={stats['failed']} "
                  f"inflight={len(dispatcher.inflight())} pending={pending}",
                  flush=True)
    except KeyboardInterrupt:
        print("dispatcher: shutting down...", flush=True)
    dispatcher.stop()
    return 0


def main(argv: list[str] | None = None, rt=None) -> int:
    parser = argparse.ArgumentParser(description="Ecosystem headless worker")
    parser.add_argument("--interval", type=float, default=60.0,
                        help="seconds between ticks (default: 60)")
    parser.add_argument("--ticks", type=int, default=0,
                        help="run N ticks then exit (default: run forever)")
    parser.add_argument("--use-postgres", action="store_true",
                        help="use PostgresTaskStore via DATABASE_URL")
    parser.add_argument("--monitor-every", type=int, default=10,
                        help="run the Etsy experiment monitor every N ticks "
                             "(default: 10; 0 disables)")
    parser.add_argument("--monitor-once", action="store_true",
                        help="run only the Etsy monitor once, then exit")
    parser.add_argument("--llm", dest="use_llm", action="store_true",
                        default=None,
                        help="force real LLM inference (requires API key)")
    parser.add_argument("--no-llm", dest="use_llm", action="store_false",
                        help="force heuristic agents (no LLM calls)")
    parser.add_argument("--no-market", dest="use_market", action="store_false",
                        default=None,
                        help="disable live market data (data-free prompts)")
    parser.add_argument("--dispatcher", action="store_true",
                        help="run the concurrent task dispatcher instead of "
                             "the tick loop: executes submitted tasks across "
                             "agents and businesses in parallel")
    parser.add_argument("--max-workers", type=int, default=4,
                        help="dispatcher threads (default: 4)")
    parser.add_argument("--max-per-agent", type=int, default=2,
                        help="dispatcher: concurrent tasks per agent type "
                             "(default: 2)")
    parser.add_argument("--max-per-business", type=int, default=2,
                        help="dispatcher: concurrent tasks per business "
                             "(default: 2)")
    parser.add_argument("--no-growth", dest="growth", action="store_false",
                        default=True,
                        help="disable the road2cissp.com growth program")
    parser.add_argument("--discovery", action="store_true",
                        help="also run the opportunity discovery pipeline "
                             "each tick (off by default: focus is road2cissp)")
    parser.add_argument("--store", action="store_true",
                        help="also run Shopify store operations (paused "
                             "project; off by default)")
    parser.add_argument("--store-optimize-every", type=int, default=30,
                        help="run the Shopify optimisation pass (spend guard, "
                             "profit, SEO, report) every N ticks (default: 30; "
                             "0 disables)")
    args = parser.parse_args(argv)

    if rt is None:
        rt = build_runtime(use_postgres=args.use_postgres, use_llm=args.use_llm,
                           use_market=args.use_market, approvals_persist=True)

    if args.dispatcher:
        return run_dispatcher(rt, args)

    if args.monitor_once:
        run_etsy_monitor_tick(rt, tick=1)
        return 0

    print(f"worker online: interval={args.interval}s "
          f"store={'postgres' if args.use_postgres else 'memory'} "
          f"focus={'road2cissp.com' if args.growth else 'none'} "
          f"discovery={'on' if args.discovery else 'off'} "
          f"shopify={'on' if args.store else 'off'} "
          f"etsy_monitor_every={args.monitor_every}", flush=True)

    tick = 0
    while True:
        tick += 1
        if args.growth:
            run_growth_tick(rt, tick)
        if args.discovery:
            try:
                result = run_tick(rt, tick)
            except Exception as exc:  # noqa: BLE001 - worker must survive ticks
                print(f"tick {tick}: ERROR {exc}", flush=True)
                result = {"tick": tick, "ok": False}
            if result["ok"]:
                print(
                    f"tick {tick}: {result['opportunities']} opps, "
                    f"{result['pursued']} pursued, winner='{result['winner']}' "
                    f"({result['score']}) → {result['business_id']} "
                    f"spend=${result['ai_spend_usd']}",
                    flush=True,
                )
            else:
                print(f"tick {tick}: FAILED {result.get('error')}", flush=True)
        if args.monitor_every and tick % args.monitor_every == 0:
            run_etsy_monitor_tick(rt, tick)
        if args.store:
            run_store_tick(rt, tick, args.store_optimize_every)
        if args.ticks and tick >= args.ticks:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
