"""Creative Agent: generates and versions creative assets.

Asset types: product images, ad concepts, social content, copy,
email campaigns. Every asset is versioned and linked to the
campaign it serves, so performance can be attributed back to
the creative that drove it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from agents.base import BaseAgent
from orchestrator.models import Task, utcnow


class CreativeAsset(BaseModel):
    id: str
    business_id: str
    campaign_id: str | None = None
    asset_type: str  # image | ad_concept | social_post | copy | email | video_concept
    title: str
    content: str  # copy text, image prompt, or asset reference
    version: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    # Performance attribution (filled by marketing agent feedback).
    impressions: int = 0
    clicks: int = 0
    conversions: int = 0


class CreativeAgent(BaseAgent):
    agent_type = "creative"
    capabilities = [
        "copywriting",
        "ad_concepts",
        "social_content",
        "email_campaigns",
    ]
    description = "Generates versioned creative assets tied to campaigns."

    def __init__(self) -> None:
        super().__init__()
        self._assets: dict[str, CreativeAsset] = {}
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"asset_{self._counter:05d}"

    def create(
        self,
        business_id: str,
        asset_type: str,
        title: str,
        content: str,
        campaign_id: str | None = None,
    ) -> CreativeAsset:
        asset = CreativeAsset(
            id=self._next_id(),
            business_id=business_id,
            campaign_id=campaign_id,
            asset_type=asset_type,
            title=title,
            content=content,
        )
        self._assets[asset.id] = asset
        return asset

    def new_version(self, asset_id: str, content: str) -> CreativeAsset:
        """Create a new version of an existing asset (immutable history)."""
        old = self._assets.get(asset_id)
        if old is None:
            raise ValueError(f"unknown asset: {asset_id}")
        new = CreativeAsset(
            id=self._next_id(),
            business_id=old.business_id,
            campaign_id=old.campaign_id,
            asset_type=old.asset_type,
            title=old.title,
            content=content,
            version=old.version + 1,
        )
        self._assets[new.id] = new
        return new

    def record_performance(
        self, asset_id: str, impressions: int, clicks: int, conversions: int
    ) -> CreativeAsset:
        asset = self._assets.get(asset_id)
        if asset is None:
            raise ValueError(f"unknown asset: {asset_id}")
        asset.impressions += impressions
        asset.clicks += clicks
        asset.conversions += conversions
        return asset

    def top_performers(self, business_id: str, limit: int = 5) -> list[CreativeAsset]:
        assets = [a for a in self._assets.values() if a.business_id == business_id]
        return sorted(assets, key=lambda a: a.conversions, reverse=True)[:limit]

    def run(self, task: Task) -> dict:
        action = task.inputs.get("action", "report")
        if action == "report":
            self.record_usage(task, tokens=200, cost_usd=0.01)
            return {
                "total_assets": len(self._assets),
                "by_type": {
                    t: sum(1 for a in self._assets.values() if a.asset_type == t)
                    for t in {a.asset_type for a in self._assets.values()}
                },
            }
        raise ValueError(f"unknown action: {action}")