"""Predictive models: rank opportunities from accumulated data.

Learns from scored opportunities and their eventual outcomes
(stored in agent memory). Calibration: compares predicted scores
to realized outcomes and reports the gap.
"""

from __future__ import annotations

from core.memory import AgentMemory, MemoryType
from core.models import Opportunity
from core.scoring import ScoringEngine


class PredictiveModel:
    def __init__(self, memory: AgentMemory | None = None) -> None:
        self.memory = memory or AgentMemory()
        self.scorer = ScoringEngine()
        # Learned adjustments per tag: tag -> average outcome delta.
        self._adjustments: dict[str, float] = {}

    def train(self) -> int:
        """Learn from experiment memories. Returns count of training samples."""
        samples = self.memory.recall(MemoryType.EXPERIMENT)
        tag_outcomes: dict[str, list[float]] = {}
        for mem in samples:
            for tag in mem.tags:
                tag_outcomes.setdefault(tag, []).append(mem.outcome_score)
        self._adjustments = {
            tag: sum(scores) / len(scores)
            for tag, scores in tag_outcomes.items()
        }
        return len(samples)

    def predict(self, opportunity: Opportunity) -> float:
        """Predicted score: base score adjusted by learned tag outcomes."""
        base = self.scorer.score(opportunity)
        if not self._adjustments:
            return base
        # Use niche as the tag key (memories are tagged by niche).
        learned = self._adjustments.get(opportunity.niche.lower(), 0.0)
        # Outcome scores are -1..1; scale to score points.
        return max(0.0, min(100.0, base + learned * 10))

    def rank(self, opportunities: list[Opportunity]) -> list[tuple[Opportunity, float]]:
        """Rank opportunities by predicted score, highest first."""
        scored = [(opp, self.predict(opp)) for opp in opportunities]
        return sorted(scored, key=lambda pair: pair[1], reverse=True)