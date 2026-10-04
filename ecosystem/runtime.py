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


def build_runtime(use_postgres: bool = False,
                  use_llm: bool | None = None,
                  use_market: bool | None = None) -> Runtime:
    """Wire every component together. Single composition root.

    use_llm: True forces real LLM sources, False forces heuristics,
    None (default) auto-detects from LLMGateway.enabled() (litellm +
    API key present). The chosen mode is logged at startup.

    use_market: True forces live market data (Etsy + Trends) into the
    LLM prompts, False disables it, None (default) enables it whenever
    the LLM gateway is on. Never breaks composition — any market
    failure degrades to data-free prompts.
    """
    import logging

    from core.llm import LLMGateway

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

    return Runtime(
        agent_registry=agent_registry,
        orchestrator=orchestrator,
        businesses=BusinessRegistry(),
        ledger=Ledger(),
        experiments=ExperimentEngine(),
        approvals=ApprovalGate(),
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
