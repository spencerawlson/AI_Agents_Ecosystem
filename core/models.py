"""Business domain models: businesses, opportunities, experiments."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from orchestrator.models import new_id, utcnow


class BusinessStatus(str, Enum):
    DISCOVERED = "discovered"
    RESEARCHING = "researching"
    VALIDATED = "validated"
    APPROVED = "approved"
    BUILDING = "building"
    TESTING = "testing"
    LAUNCHED = "launched"
    OPERATING = "operating"
    OPTIMIZING = "optimizing"
    SCALING = "scaling"
    PAUSED = "paused"
    MATURE = "mature"
    TERMINATED = "terminated"


# Valid state transitions. TERMINATED is reachable from most states;
# it is omitted here for brevity and handled explicitly in the registry.
TRANSITIONS: dict[BusinessStatus, list[BusinessStatus]] = {
    BusinessStatus.DISCOVERED: [BusinessStatus.RESEARCHING],
    BusinessStatus.RESEARCHING: [BusinessStatus.VALIDATED],
    BusinessStatus.VALIDATED: [BusinessStatus.APPROVED],
    BusinessStatus.APPROVED: [BusinessStatus.BUILDING],
    BusinessStatus.BUILDING: [BusinessStatus.TESTING],
    BusinessStatus.TESTING: [BusinessStatus.LAUNCHED],
    BusinessStatus.LAUNCHED: [BusinessStatus.OPERATING],
    BusinessStatus.OPERATING: [BusinessStatus.OPTIMIZING, BusinessStatus.PAUSED],
    BusinessStatus.OPTIMIZING: [BusinessStatus.SCALING, BusinessStatus.PAUSED],
    BusinessStatus.SCALING: [BusinessStatus.MATURE, BusinessStatus.PAUSED],
    BusinessStatus.PAUSED: [BusinessStatus.OPERATING, BusinessStatus.OPTIMIZING],
    BusinessStatus.MATURE: [],
    BusinessStatus.TERMINATED: [],
}


class Business(BaseModel):
    id: str = Field(default_factory=lambda: new_id("biz"))
    name: str
    business_type: str
    status: BusinessStatus = BusinessStatus.DISCOVERED
    marketplace: str | None = None
    domain: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Opportunity(BaseModel):
    """A structured business opportunity from discovery/research."""

    id: str = Field(default_factory=lambda: new_id("opp"))
    niche: str
    business_type: str
    demand_score: float = 0.0       # 0..1
    competition_score: float = 0.0  # 0..1, higher = more competitive
    expected_margin: float = 0.0    # 0..1
    startup_cost_usd: float = 0.0
    advertising_cost_usd: float = 0.0
    operational_complexity: float = 0.0  # 0..1, higher = harder
    automation_potential: float = 0.0    # 0..1
    scalability: float = 0.0             # 0..1
    recurring_revenue: float = 0.0       # 0..1
    marketplace_risk: float = 0.0        # 0..1
    supplier_risk: float = 0.0           # 0..1
    support_burden: float = 0.0          # 0..1
    time_to_market_days: int = 0
    score: float | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ExperimentStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    EVALUATING = "evaluating"
    COMPLETED = "completed"


class Experiment(BaseModel):
    """A business treated as a bounded experiment."""

    id: str = Field(default_factory=lambda: new_id("exp"))
    business_id: str
    initial_capital_usd: float
    max_advertising_usd: float
    duration_days: int
    target_cac_max: float | None = None
    target_conversion_min: float | None = None
    target_gross_margin_min: float | None = None
    target_roas_min: float | None = None
    max_refund_rate: float | None = None
    status: ExperimentStatus = ExperimentStatus.PLANNED
    recommendation: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class LedgerEntry(BaseModel):
    """One financial line item for a business."""

    id: str = Field(default_factory=lambda: new_id("txn"))
    business_id: str
    kind: str  # revenue | cogs | shipping | marketplace_fee | payment_processing
               # advertising | refund | chargeback | hosting | api_cost
               # ai_inference | subscription | other_expense
    amount_usd: float  # positive for revenue, negative for costs
    description: str = ""
    created_at: datetime = Field(default_factory=utcnow)