from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.agent_runner import ReportingAgentRunner
from manyselves.core.reporting.config import load_packaged_agents
from manyselves.core.reporting.skills.service import (
    ProductSkillEvolutionService,
    ProjectSkillEvolutionService,
)
from manyselves.core.tools.registry import ToolRegistry
from manyselves.core.tools.skill_evolution_tools import ProductSkillEvolutionTool


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
        allowed_outputs=["skill_evolution_submission"],
    )

    tools = runner._tools(definition, envelope, "session", "workflow")

    assert tools.get("product_skill_evolution") is not None
    assert tools.get("inspect_document") is not None


@pytest.mark.asyncio
async def test_product_skill_tool_contract_supports_cross_module_skill(tmp_path: Path) -> None:
    tool = ProductSkillEvolutionTool(tmp_path)
    registry = ToolRegistry()
    registry.register(tool)
    schema = registry.get_definitions()[0]["input_schema"]

    assert "metadata" not in schema["properties"]
    assert set(schema["properties"]["module_id"]["enum"]) == {
        "2.1",
        "2.2",
        "2.3",
        "2.4",
        "2.5",
        "all",
    }

    feedback = await tool(
        action="record_feedback",
        skill_id="report-template-writing",
        module_id="all",
        feedback="统一五个模块的报告写作方法",
        report_version_id="initial",
        explicit_promotion_requested=True,
    )
    candidate = await tool(
        action="propose",
        feedback_id=feedback["id"],
        title="配电报告模板写作技能",
        submodules=["all"],
        proposed_content="证据、判断、原因、风险与行动形成闭环。",
        reason="形成跨模块统一写作规则",
    )

    assert candidate["feedback_id"] == feedback["id"]
    assert candidate["module_id"] == "all"
    assert candidate["submodules"] == ["all"]


@pytest.mark.asyncio
async def test_product_skill_tool_error_names_the_actual_propose_parameters(
    tmp_path: Path,
) -> None:
    tool = ProductSkillEvolutionTool(tmp_path)

    with pytest.raises(
        ValueError,
        match=(
            "feedback_id, title, submodules, proposed_content, reason"
        ),
    ):
        await tool(
            action="propose",
            title="缺少真实依赖",
            proposed_content="候选正文",
            reason="回归日志参数",
        )
