"""Real LLM inference gateway for the ecosystem.

Routes cheap vs smart model calls through litellm. Every call is
fallback-safe: LLMUnavailable is raised on any configuration or API
failure, and callers catch it to degrade to heuristics — an LLM
failure must NEVER crash a tick.

Env config:
    ECOSYSTEM_CHEAP_MODEL  default "gpt-6-luna"
    ECOSYSTEM_SMART_MODEL  default "gpt-6.1-sol"
    GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY  (at least one)
"""

from __future__ import annotations

import json
import os
from typing import Any


class LLMUnavailable(RuntimeError):
    """Raised when real LLM inference cannot be used.

    Callers must catch this and fall back to heuristic behavior.
    """


class LLMGateway:
    """Thin wrapper over litellm with tier-based model routing."""

    CHEAP = "cheap"
    SMART = "smart"

    def __init__(
        self,
        cheap_model: str | None = None,
        smart_model: str | None = None,
        timeout: int = 60,
    ) -> None:
        self.cheap_model = cheap_model or os.environ.get(
            "ECOSYSTEM_CHEAP_MODEL", "gpt-6-luna")
        self.smart_model = smart_model or os.environ.get(
            "ECOSYSTEM_SMART_MODEL", "gpt-6.1-sol")
        self.timeout = timeout
        self._litellm: Any = None

    # -- availability --------------------------------------------------

    @staticmethod
    def enabled() -> bool:
        """True only if litellm imports AND a plausible key env var is set."""
        try:
            import litellm  # noqa: F401
        except ImportError:
            return False
        return any(os.environ.get(k) for k in (
            "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))

    def _require_litellm(self) -> Any:
        if self._litellm is None:
            try:
                import litellm
            except ImportError as exc:
                raise LLMUnavailable(
                    "litellm is not installed (pip install litellm)") from exc
            if not any(os.environ.get(k) for k in (
                    "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")):
                raise LLMUnavailable(
                    "no LLM API key configured "
                    "(GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY)")
            self._litellm = litellm
        return self._litellm

    # -- inference -----------------------------------------------------

    def model_for(self, tier: str) -> str:
        return self.smart_model if tier == self.SMART else self.cheap_model

    def complete(
        self,
        prompt: str,
        tier: str = CHEAP,
        system: str | None = None,
        json_mode: bool = True,
    ) -> dict:
        """Run one completion. Returns {text, json, model, input_tokens,
        output_tokens, cost_usd}. Raises LLMUnavailable on any failure."""
        litellm = self._require_litellm()
        model = self.model_for(tier)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = litellm.completion(model=model, messages=messages, **kwargs)
        except Exception as exc:
            raise LLMUnavailable(f"LLM call failed ({model}): {exc}") from exc
        try:
            text = resp.choices[0].message.content or ""
            usage = resp.usage
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            cost_usd = float(litellm.completion_cost(resp))
        except Exception as exc:
            raise LLMUnavailable(f"could not parse LLM response: {exc}") from exc
        parsed: Any = None
        if json_mode:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise LLMUnavailable(
                    f"LLM did not return valid JSON: {text[:200]}") from exc
        return {
            "text": text,
            "json": parsed,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }
