"""Shared runtime wiring for the AI Agents Ecosystem.

Builds the full application composition root in one place so the
dashboard, the worker loop, and tests all run against the same real
components: orchestrator, agent registry, business registry, ledger,
experiment engine, approval gate, and audit log.

Usage:
    from ecosystem.runtime import build_runtime
    rt = build_runtime()                    # in-memory stores
    rt = build_runtime(use_postgres=True)   # PostgresTaskStore via DATABASE_URL
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agents.creative.agent import CreativeAgent
from agents.discovery.agent import DiscoveryAgent
from agents.marketing.agent import MarketingAgent
from agents.operations.agent import OperationsAgent
from agents.research.agent import ResearchAgent
from agents.review.agent import ReviewAgent
from agents.support.agent import SupportAgent
from core.audit import AuditLog
from core.experiments import ExperimentEngine
from core.ledger import Ledger
from core.registry import BusinessRegistry
from orchestrator.approvals import ApprovalGate
from orchestrator.engine import Orchestrator
from orchestrator.registry import AgentRegistry

# Every real agent class the runtime can dispatch to.
AGENT_CLASSES = (
    DiscoveryAgent,
    ResearchAgent,
    MarketingAgent,
    SupportAgent,
    CreativeAgent,
    OperationsAgent,
    ReviewAgent,
)


def _build_agent(cls: type, gateway: "LLMGateway | None",
                 market: "MarketDataProvider | None" = None):
    """Construct an agent, wiring LLM sources when the gateway is on.

    market (live Etsy + Trends data) is injected into the LLM sources so
    prompts carry real numbers; silently ignored when unavailable.
    """
    from core.llm import LLMGateway  # noqa: F401  (re-export for type hints)

    if gateway is not None and cls is DiscoveryAgent:
        from agents.discovery.llm_source import LLMOpportunitySource
        return cls(source=LLMOpportunitySource(gateway, market=market))
    if gateway is not None and cls is ResearchAgent:
        from agents.research.llm_source import LLMResearchSource
        return cls(source=LLMResearchSource(gateway, market=market))
    return cls()


@dataclass
class Runtime:
    agent_registry: AgentRegistry
    orchestrator: Orchestrator
    businesses: BusinessRegistry
    ledger: Ledger
    experiments: ExperimentEngine
    approvals: ApprovalGate
    audit: AuditLog
    handlers: dict


def default_approvals_path() -> Path:
    """Where approvals persist so the dashboard GUI and mission scripts
    (separate processes) see the same approval queue."""
    return Path(__file__).resolve().parent.parent / "data" / "approvals.json"


_dotenv_loaded = False


def _load_dotenv() -> None:
    """Load the repo's .env file into os.environ (once per process).

    The ecosystem's config — OPENAI_API_KEY, ECOSYSTEM_STATE_DB,
    DATABASE_URL, APPROVALS_PATH — used to live only in whichever
    shell happened to launch a process, so the dashboard, the worker
    and the missions kept starting in different modes depending on
    which terminal started them (an evening of FedServ runs in
    2026-10-18 turned on exactly this). A .env file at the repo root
    (gitignored, never committed) is now read by every entry point
    through build_runtime. Variables already set in the real
    environment always win — the file only fills gaps.
    ECOSYSTEM_ENV_FILE overrides the file location.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    path = Path(os.environ.get("ECOSYSTEM_ENV_FILE")
                or Path(__file__).resolve().parent.parent / ".env")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def build_runtime(use_postgres: bool = False,
                  use_llm: bool | None = None,
                  use_market: bool | None = None,
                  approvals_persist: str | Path | bool | None = None) -> Runtime:
    """Wire every component together. Single composition root.

    use_llm: True forces real LLM sources, False forces heuristics,
    None (default) auto-detects from LLMGateway.enabled() (litellm +
    API key present). The chosen mode is logged at startup.

    use_market: True forces live market data (Etsy + Trends) into the
    LLM prompts, False disables it, None (default) enables it whenever
    the LLM gateway is on. Never breaks composition — any market
    failure degrades to data-free prompts.

    approvals_persist: True (or a path) persists the approval gate to
    disk so separate processes (dashboard GUI, mission scripts) share
    one approval queue. None (default) keeps the gate in-memory, which
    is what tests want. The APPROVALS_PATH env var is a fallback.
    """
    import logging

    from core.llm import LLMGateway

    _load_dotenv()
    log = logging.getLogger("ecosystem.runtime")
    llm_on = LLMGateway.enabled() if use_llm is None else bool(use_llm)
    gateway = LLMGateway() if llm_on else None

    market = None
    market_on = llm_on and (True if use_market is None else bool(use_market))
    if market_on:
        from core.market_data import build_market_provider

        try:
            market = build_market_provider()
        except Exception as exc:  # never break composition
            log.warning("market data disabled (%s)", exc)
            market = None
        if market is None:
            market_on = False

    agent_registry = AgentRegistry()
    handlers: dict = {}
    for cls in AGENT_CLASSES:
        agent_registry.register(
            cls.agent_type,
            capabilities=list(cls.capabilities),
            description=cls.description,
        )
        handlers[cls.agent_type] = _build_agent(cls, gateway, market)

    log.warning("runtime mode: LLM %s (%s) | market data %s",
                "ON" if llm_on else "OFF (heuristics)",
                gateway.cheap_model if gateway else "no key/litellm",
                "ON" if market_on else "OFF")
    if use_llm and not LLMGateway.enabled():
        # --llm was forced but the gateway can't actually run: say so now
        # instead of letting the banner claim "LLM ON" and failing mid-run.
        log.warning("WARNING: --llm requested but LLMGateway.enabled() is "
                    "False (litellm not installed or no API key in env) — "
                    "LLM actions will fail at dispatch")

    store = None
    if use_postgres:
        from infra.pg_store import PostgresTaskStore

        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "use_postgres=True requires DATABASE_URL "
                "(e.g. postgresql://ecosystem:ecosystem@localhost:5432/ecosystem). "
                "Run: docker compose -f infra/docker-compose.yml up -d && python launch.py initdb"
            )
        store = PostgresTaskStore(url)

    audit_log = AuditLog()
    orchestrator = Orchestrator(registry=agent_registry, store=store,
                              audit=audit_log)
    for agent_type, handler in handlers.items():
        orchestrator.register_handler(agent_type, handler)

    persist = approvals_persist
    if persist is None:
        persist = os.environ.get("APPROVALS_PATH")
    if persist is True:
        persist = default_approvals_path()

    # Shared state: with ECOSYSTEM_STATE_DB set, businesses and the
    # ledger live in one SQLite file shared by every process (dashboard,
    # worker, missions) instead of per-process memory — real businesses
    # become visible in the GUI and survive restarts. Unset = the
    # original in-memory behavior (tests stay hermetic).
    state_db = os.environ.get("ECOSYSTEM_STATE_DB")
    if state_db:
        from core.state_store import SharedBusinessRegistry, SharedLedger

        businesses: BusinessRegistry = SharedBusinessRegistry(state_db)
        ledger: Ledger = SharedLedger(state_db)
        log.info("shared state: businesses + ledger in %s", state_db)
    else:
        businesses = BusinessRegistry()
        ledger = Ledger()

    return Runtime(
        agent_registry=agent_registry,
        orchestrator=orchestrator,
        businesses=businesses,
        ledger=ledger,
        experiments=ExperimentEngine(),
        approvals=ApprovalGate(persist_path=Path(persist) if persist else None),
        audit=audit_log,
        handlers=handlers,
    )


def create_dashboard_app(rt: Runtime | None = None):
    """Build the FastAPI dashboard bound to a runtime."""
    from dashboard.app import create_app

    rt = rt or build_runtime(use_postgres=bool(os.environ.get("DATABASE_URL")))
    app = create_app(
        orchestrator=rt.orchestrator,
        registry=rt.businesses,
        ledger=rt.ledger,
        experiments=rt.experiments,
        approvals=rt.approvals,
        audit=rt.audit,
    )
    app.state.runtime_holder = {"rt": rt}
    return app
