"""Peer-review agent: the fresh-eyes layer of the quintuple check.

ReviewAgent never produces the work — it critiques someone else's output
against the task's acceptance criteria, looking for exactly the defects a
producer goes blind to: unmet criteria, empty or placeholder values,
outputs that technically ran but say nothing.

Heuristic today (keyword coverage, placeholder detection, substance
checks); the review() entry point is LLM-upgradeable without changing the
verification pipeline.
"""

from __future__ import annotations

import json
import re

from agents.base import BaseAgent
from orchestrator.models import Task

_STOPWORDS = {
    "the", "a", "an", "and", "or", "with", "must", "have", "has", "are",
    "that", "this", "from", "into", "each", "than", "then", "will", "should",
    "output", "include", "includes", "containing", "least",
}

_PLACEHOLDERS = re.compile(
    r"\b(todo|tbd|lorem ipsum|placeholder|xxx+|asdf)\b|\.\.\.(?!\w)", re.IGNORECASE
)


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z]{4,}", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _flatten(output: dict) -> str:
    try:
        return json.dumps(output, default=str).lower()
    except Exception:  # noqa: BLE001
        return str(output).lower()


def _has_substance(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and not _PLACEHOLDERS.search(value)
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0 and any(_has_substance(v) for v in value)
    if isinstance(value, dict):
        return len(value) > 0 and any(_has_substance(v) for v in value.values())
    return True


class ReviewAgent(BaseAgent):
    """Critiques another agent's output. Never produces original work."""

    agent_type = "review"
    capabilities = ["peer_review", "quality_audit"]
    description = "Independently reviews agent outputs against acceptance criteria."

    def run(self, task: Task) -> dict:
        """Direct dispatch: expects inputs {target_task, target_output}."""
        target = task.inputs.get("target_output", {})
        criteria = task.inputs.get("acceptance_criteria", [])
        issues = self.review(task, target if isinstance(target, dict) else {})
        return {"issues": issues, "criteria_checked": len(criteria),
                "passed": not issues}

    def review(self, task: Task, output: dict) -> list[str]:
        """Peer-review layer: return issue strings, empty when the output holds up."""
        issues: list[str] = []
        blob = _flatten(output)
        criteria: list[str] = list(getattr(task, "acceptance_criteria", None) or [])

        # 1. Acceptance-criteria coverage: every criterion's substance words
        #    should appear in the output. A criterion the output never even
        #    mentions is a criterion the producer ignored.
        for criterion in criteria:
            keys = _keywords(criterion)
            if not keys:
                continue
            hit = sum(1 for k in keys if k in blob)
            if hit / len(keys) < 0.5:
                issues.append(
                    f"acceptance criterion not evidenced in output: {criterion!r}"
                )

        # 2. Placeholder / empty detection.
        if _PLACEHOLDERS.search(blob):
            issues.append("output contains placeholder or filler text")
        empty_top = [k for k, v in output.items() if not _has_substance(v)]
        if empty_top:
            issues.append(
                f"output has empty or placeholder top-level fields: {empty_top}"
            )

        # 3. Substance: an output that ran but says nothing fails review.
        substantive = [k for k, v in output.items() if _has_substance(v)]
        if not substantive:
            issues.append("output has no substantive content")
        elif len(substantive) == 1 and len(blob) < 60:
            issues.append(
                f"output is suspiciously thin (single field {substantive[0]!r})"
            )

        return issues
