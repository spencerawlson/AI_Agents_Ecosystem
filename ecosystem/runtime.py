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
)


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


def build_runtime(use_postgres: bool = False) -> Runtime:
    """Wire every component together. Single composition root."""
    agent_registry = AgentRegistry()
    handlers: dict = {}
    for cls in AGENT_CLASSES:
        agent_registry.register(
            cls.agent_type,
            capabilities=list(cls.capabilities),
            description=cls.description,
        )
        handlers[cls.agent_type] = cls()

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

    orchestrator = Orchestrator(registry=agent_registry, store=store)
    for agent_type, handler in handlers.items():
        orchestrator.register_handler(agent_type, handler)

    return Runtime(
        agent_registry=agent_registry,
        orchestrator=orchestrator,
        businesses=BusinessRegistry(),
        ledger=Ledger(),
        experiments=ExperimentEngine(),
        approvals=ApprovalGate(),
        audit=AuditLog(),
        handlers=handlers,
    )


def create_dashboard_app(rt: Runtime | None = None):
    """Build the FastAPI dashboard bound to a runtime."""
    from dashboard.app import create_app

    rt = rt or build_runtime(use_postgres=bool(os.environ.get("DATABASE_URL")))
    return create_app(
        orchestrator=rt.orchestrator,
        registry=rt.businesses,
        ledger=rt.ledger,
        experiments=rt.experiments,
        approvals=rt.approvals,
        audit=rt.audit,
    )
