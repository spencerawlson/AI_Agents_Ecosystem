"""Agent registry: capability-based lookup of agent types.

Agents register under a type name with the capabilities they provide.
The orchestrator resolves a task's required capability to a concrete
agent type.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentRegistration:
    agent_type: str
    capabilities: list[str] = field(default_factory=list)
    description: str = ""


class AgentRegistry:
    def __init__(self) -> None:
        self._by_type: dict[str, AgentRegistration] = {}
        self._by_capability: dict[str, list[str]] = {}

    def register(
        self,
        agent_type: str,
        capabilities: list[str] | None = None,
        description: str = "",
    ) -> None:
        reg = AgentRegistration(
            agent_type=agent_type,
            capabilities=capabilities or [],
            description=description,
        )
        self._by_type[agent_type] = reg
        for cap in reg.capabilities:
            self._by_capability.setdefault(cap, []).append(agent_type)

    def get(self, agent_type: str) -> AgentRegistration | None:
        return self._by_type.get(agent_type)

    def resolve(self, capability: str) -> list[str]:
        """Return agent types that provide a capability."""
        return list(self._by_capability.get(capability, []))

    def agent_types(self) -> list[str]:
        return sorted(self._by_type.keys())

    def unregister(self, agent_type: str) -> None:
        reg = self._by_type.pop(agent_type, None)
        if reg is None:
            return
        for cap in reg.capabilities:
            types = self._by_capability.get(cap, [])
            if agent_type in types:
                types.remove(agent_type)
