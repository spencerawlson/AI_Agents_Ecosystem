"""Research agent tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.research.agent import OpportunityReport, ResearchAgent
from orchestrator.models import Task


def test_research_agent():
    agent = ResearchAgent()
    task = Task(
        agent_type="research",
        inputs={"opportunities": [
            {"id": "opp_1", "niche": "test niche", "business_type": "digital_product",
             "expected_margin": 0.8},
        ]},
        budget_usd=5.0,
        budget_tokens=10000,
    )
    output = agent(task)
    assert output["count"] == 1
    report = output["reports"][0]
    assert report["niche"] == "test niche"
    assert report["verdict"] in ("pursue", "watch", "reject")
    # Validates against the Pydantic schema without error.
    OpportunityReport.model_validate(report)
    print("Research agent tests passed.")


if __name__ == "__main__":
    test_research_agent()
