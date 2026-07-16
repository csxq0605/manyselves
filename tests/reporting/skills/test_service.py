from pathlib import Path

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.agent_runner import ReportingAgentRunner
from manyselves.core.reporting.agentic_models import TaskEnvelope
from manyselves.core.reporting.config import load_packaged_agents
from manyselves.core.reporting.skills.service import (
    ProductSkillEvolutionService,
    ProjectSkillEvolutionService,
)


def test_project_and_product_services_use_separate_roots_and_identities(tmp_path: Path) -> None:
    project = ProjectSkillEvolutionService(tmp_path)
    product = ProductSkillEvolutionService(tmp_path, actor_id="product-skill-maintainer")

    assert project.store.root == tmp_path / "Capabilities/skills"
    assert product.store.root == tmp_path / "ProductCapabilities/skills"
    with pytest.raises(ValueError, match="product-skill-maintainer"):
        ProductSkillEvolutionService(tmp_path, actor_id="main-agent")


def test_product_publication_tool_is_only_on_product_maintainer(tmp_path: Path) -> None:
    class Provider(LLMProvider):
        def __init__(self):
            super().__init__("test", model="never-called")

        async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
            raise AssertionError

    runner = ReportingAgentRunner(
        tmp_path,
        MessageBus(),
        Provider(),
        AgentDefaults(),
        product_skill_root=tmp_path / "ProductCapabilities/skills",
    )
    definition = load_packaged_agents()["product-skill-maintainer"]
    envelope = TaskEnvelope(
        task_id="product-skill",
        run_id="run-skill",
        agent_id=definition.id,
        objective="维护产品 Skill",
    )

    tools = runner._tools(definition, envelope, "session", "workflow")

    assert tools.get("product_skill_evolution") is not None
