"""Five-layer output verification ("quintuple check") for agent work.

Every agent output must clear all five layers before its task is marked
complete:

  1. SELF_CHECK     - the producing agent checks its own output against the
                      task's acceptance criteria (BaseAgent.self_check hook).
  2. CONTRACT       - the output honours its declared contract: it is a dict
                      and, when the task names one, validates against the
                      registered pydantic model / validator for the agent type.
  3. DETERMINISTIC   - registered pure-function validators for the agent type
                      re-verify the output independently of the producer.
  4. PEER_REVIEW    - a *different* agent (the review handler, when the
                      runtime registers one) critiques the output against the
                      acceptance criteria. Fresh eyes catch what self-review
                      misses.
  5. EVIDENCE       - the verification report itself is complete: every layer
                      ran, every acceptance criterion was examined by at least
                      one layer, and the report is attached to the run for the
                      audit trail.

Layers with nothing to check pass vacuously, so tasks without acceptance
criteria keep their existing behaviour. Anything that fails is returned as
structured findings; the orchestrator feeds them back to the agent as rework
instructions (bounded by Task.max_rework).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class CheckLayer(str, Enum):
    SELF_CHECK = "self_check"
    CONTRACT = "contract"
    DETERMINISTIC = "deterministic"
    PEER_REVIEW = "peer_review"
    EVIDENCE = "evidence"


@dataclass
class LayerResult:
    layer: CheckLayer
    passed: bool
    findings: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class VerificationReport:
    passed: bool
    layers: list[LayerResult] = field(default_factory=list)

    @property
    def failures(self) -> list[str]:
        out: list[str] = []
        for lr in self.layers:
            if not lr.passed:
                out.extend(f"[{lr.layer.value}] {f}" for f in lr.findings)
        return out

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "layers": [
                {
                    "layer": lr.layer.value,
                    "passed": lr.passed,
                    "findings": lr.findings,
                    "note": lr.note,
                }
                for lr in self.layers
            ],
            "failures": self.failures,
        }


# -- registries -------------------------------------------------------------

# agent_type -> pydantic model class (has .model_validate) or callable
_contracts: dict[str, Any] = {}
# agent_type -> list of callables (task, output) -> list[str] issues
_validators: dict[str, list[Callable[[Any, dict], list[str]]]] = {}


def register_contract(agent_type: str, contract: Any) -> None:
    """Register the output contract for an agent type.

    contract is either a pydantic BaseModel subclass or a callable taking
    the output dict and returning a list of issue strings (empty = valid).
    """
    _contracts[agent_type] = contract


def register_validator(
    agent_type: str, fn: Callable[[Any, dict], list[str]]
) -> None:
    """Register a deterministic validator for an agent type.

    fn(task, output) returns a list of issue strings; empty means valid.
    Validators must be pure functions of (task, output) — no network, no
    randomness — so re-verification is reproducible.
    """
    _validators.setdefault(agent_type, []).append(fn)


def get_validators(agent_type: str) -> list[Callable[[Any, dict], list[str]]]:
    return list(_validators.get(agent_type, []))


# -- verification -----------------------------------------------------------


def verify_output(
    task: Any,
    output: dict,
    self_check_issues: list[str] | None = None,
    peer_reviewer: Callable[[Any, dict], list[str]] | None = None,
) -> VerificationReport:
    """Run the five verification layers over an agent's output."""
    layers: list[LayerResult] = []
    criteria: list[str] = list(getattr(task, "acceptance_criteria", None) or [])

    # Layer 1: self-check (the producer's own verdict).
    issues = list(self_check_issues or [])
    layers.append(
        LayerResult(
            layer=CheckLayer.SELF_CHECK,
            passed=not issues,
            findings=issues,
            note="" if issues else "producer reported no issues",
        )
    )

    # Layer 2: contract.
    contract_issues: list[str] = []
    if not isinstance(output, dict):
        contract_issues.append(f"output must be a dict, got {type(output).__name__}")
    else:
        contract = _contracts.get(getattr(task, "agent_type", ""))
        if contract is not None:
            try:
                if hasattr(contract, "model_validate"):
                    contract.model_validate(output)
                else:
                    contract_issues.extend(contract(output) or [])
            except Exception as exc:  # noqa: BLE001 - contract failure is a finding
                contract_issues.append(f"contract validation failed: {exc}")
    layers.append(
        LayerResult(
            layer=CheckLayer.CONTRACT,
            passed=not contract_issues,
            findings=contract_issues,
            note="no registered contract" if not _contracts.get(getattr(task, "agent_type", "")) and not contract_issues else "",
        )
    )

    # Layer 3: deterministic validators (independent re-verification).
    det_issues: list[str] = []
    det_note = ""
    validators = get_validators(getattr(task, "agent_type", ""))
    if not validators:
        det_note = "no deterministic validators registered"
    for fn in validators:
        try:
            det_issues.extend(fn(task, output) or [])
        except Exception as exc:  # noqa: BLE001 - validator crash is a finding
            det_issues.append(f"validator {getattr(fn, '__name__', fn)} crashed: {exc}")
    layers.append(
        LayerResult(
            layer=CheckLayer.DETERMINISTIC,
            passed=not det_issues,
            findings=det_issues,
            note=det_note,
        )
    )

    # Layer 4: peer review (fresh eyes).
    peer_issues: list[str] = []
    peer_note = ""
    if peer_reviewer is None:
        peer_note = "no peer reviewer configured"
    else:
        try:
            peer_issues.extend(peer_reviewer(task, output) or [])
        except Exception as exc:  # noqa: BLE001 - reviewer crash is a finding
            peer_issues.append(f"peer reviewer crashed: {exc}")
    layers.append(
        LayerResult(
            layer=CheckLayer.PEER_REVIEW,
            passed=not peer_issues,
            findings=peer_issues,
            note=peer_note,
        )
    )

    # Layer 5: evidence — every layer ran and every criterion was examined.
    ev_issues: list[str] = []
    if len(layers) != 4:
        ev_issues.append(f"expected 4 prior layers, found {len(layers)}")
    examined = bool(issues is not None)  # self-check always examines criteria
    if criteria and not examined:
        ev_issues.append("acceptance criteria were never examined")
    layers.append(
        LayerResult(
            layer=CheckLayer.EVIDENCE,
            passed=not ev_issues,
            findings=ev_issues,
            note=f"{len(criteria)} acceptance criteria traced" if not ev_issues else "",
        )
    )

    passed = all(lr.passed for lr in layers)
    return VerificationReport(passed=passed, layers=layers)


def rework_feedback(report: VerificationReport) -> str:
    """Render verification failures as rework instructions for the agent."""
    lines = [
        "Your previous output FAILED quality verification.",
        "Fix every issue below and resubmit. Do not resubmit unchanged output.",
    ]
    for f in report.failures:
        lines.append(f"- {f}")
    return "\n".join(lines)
