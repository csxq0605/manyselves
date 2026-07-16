from pathlib import Path

from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.agent_runner import ReportingAgentRunner
from manyselves.core.reporting.module_skills import ModuleSkillLibrary
from manyselves.core.reporting.skills.governance import SkillGovernanceStore
from manyselves.core.reporting.skills.resolver import RuntimeSkillResolver


class NeverCalledProvider(LLMProvider):
    def __init__(self):
        super().__init__("test", model="never-called")

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        raise AssertionError("resolver test does not execute Agents")


def _publish(root: Path, scope: str, content: str):
    store = SkillGovernanceStore(root, scope=scope)
    feedback = store.record_feedback(
        skill_id="pds.module24.device-risk",
        module_id="2.4",
        feedback="改进设备规则",
        explicit_promotion_requested=True,
        report_version_id="report-v1",
    )
    candidate = store.create_candidate(
        feedback.id,
        title="设备状态与风险",
        submodules=["2.4.1.1", "2.4.1.2"],
        proposed_content=content,
        reason="可复用改进",
    )
    evaluation = store.record_evaluation(
        candidate.id,
        baseline=0.5,
        candidate_score=0.9,
        regressions=[],
        model="test",
    )
    return store.publish(candidate.id, evaluation.id, confirmed=True)


def test_runtime_precedence_is_packaged_then_product_then_project(tmp_path: Path) -> None:
    product_root = tmp_path / "product-skills"
    project_root = tmp_path / "project-skills"
    _publish(product_root, "product", "产品规则")
    project_version = _publish(project_root, "project", "项目规则")

    library = RuntimeSkillResolver.resolve(
        ModuleSkillLibrary.packaged(),
        product_root=product_root,
        project_root=project_root,
    )
    skill = next(item for item in library.skills if item.id == "pds.module24.device-risk")

    assert skill.content == "项目规则"
    assert skill.version == project_version.id
    assert skill.scope == "project"


def test_agent_runner_loads_active_project_skill_for_later_runs(tmp_path: Path) -> None:
    project_root = tmp_path / "Capabilities/skills"
    active = _publish(project_root, "project", "后续运行必须加载此项目规则")

    runner = ReportingAgentRunner(
        tmp_path,
        MessageBus(),
        NeverCalledProvider(),
        AgentDefaults(),
        product_skill_root=tmp_path / "ProductCapabilities/skills",
    )
    skill = next(item for item in runner.module_skills.skills if item.id == active.skill_id)

    assert skill.content == "后续运行必须加载此项目规则"
    provenance = {item.skill_id: item for item in runner.skill_provenance()}
    assert provenance[active.skill_id].version == active.id
    assert provenance[active.skill_id].scope == "project"


def test_rollback_changes_new_runners_without_mutating_old_provenance(tmp_path: Path) -> None:
    project_root = tmp_path / "Capabilities/skills"
    first = _publish(project_root, "project", "项目规则第一版")
    second = _publish(project_root, "project", "项目规则第二版")
    before = ReportingAgentRunner(
        tmp_path,
        MessageBus(),
        NeverCalledProvider(),
        AgentDefaults(),
        product_skill_root=tmp_path / "ProductCapabilities/skills",
    )
    recorded_before = {item.skill_id: item for item in before.skill_provenance()}[second.skill_id]

    SkillGovernanceStore(project_root, scope="project").rollback(
        skill_id=first.skill_id, version_id=first.id
    )
    after = ReportingAgentRunner(
        tmp_path,
        MessageBus(),
        NeverCalledProvider(),
        AgentDefaults(),
        product_skill_root=tmp_path / "ProductCapabilities/skills",
    )
    recorded_after = {item.skill_id: item for item in after.skill_provenance()}[first.skill_id]

    assert recorded_before.version == second.id
    assert recorded_after.version == first.id
    assert recorded_before.version == second.id
