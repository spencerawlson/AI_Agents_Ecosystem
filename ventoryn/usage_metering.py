"""Axionory Phase 4: record every billable LLM call made during a
customer job.

core/llm.py calls back through core.llm.set_usage_hook — core has no
dependency on ventoryn; ventoryn registers record_llm_usage as that
hook once, at app startup (see ventoryn/app.py).

Only calls made INSIDE a ``collecting_usage()`` block are recorded —
the operator's own internal businesses (agents/*, core/ads.py, etc.,
invoked outside any customer job) call the same LLMGateway and are
never metered or billed, by construction: record_llm_usage is a
no-op when no collector is active.

This phase only records what happened — provider_cost_cents and
pricing_version are not computed here (Phase 5's pricing engine does
that); customer_charge_cents stays unset until priced.
"""

from __future__ import annotations

import contextvars

_collector: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "axionory_usage_collector", default=None)


class collecting_usage:
    """Context manager: collects every LLM call made inside the
    block. Wrap exactly one job's execution (site_jobs.run) in it so
    each collected event can be persisted keyed to that job's id."""

    def __enter__(self) -> list[dict]:
        self._events: list[dict] = []
        self._token = _collector.set(self._events)
        return self._events

    def __exit__(self, *exc) -> bool:
        _collector.reset(self._token)
        return False


def record_llm_usage(info: dict) -> None:
    """The core.llm usage-hook target. A no-op outside an active
    collecting_usage() block."""
    events = _collector.get()
    if events is not None:
        events.append(dict(info))


def _provider_for(model: str) -> str:
    """Best-effort provider tag from litellm's 'provider/model'
    convention; '' when the model string carries no such prefix —
    never guessed beyond what the string actually says."""
    return model.split("/", 1)[0] if "/" in model else ""


def persist_usage_events(store, events: list[dict], *, account_id: str,
                         job_id: str, workspace_id: str = "",
                         site_id: str = "", campaign_id: str = "") -> int:
    """Persist collected LLM-call events for one job, keyed to
    ``job_id``. Idempotent: a job_id that already has recorded events
    is skipped entirely, so a retried persist call (e.g. a retried
    webhook or a crash-and-resume) never double-records. Returns how
    many events were newly recorded."""
    if not events or not job_id:
        return 0
    if store.usage_events_for_job(job_id):
        return 0
    for event in events:
        model = event.get("model") or ""
        store.record_usage_event(
            account_id=account_id, workspace_id=workspace_id,
            site_id=site_id, job_id=job_id, campaign_id=campaign_id,
            provider=_provider_for(model), model=model,
            input_tokens=int(event.get("input_tokens") or 0),
            output_tokens=int(event.get("output_tokens") or 0),
            provider_cost_cents=round(float(event.get("cost_usd") or 0) * 100))
    return len(events)
