from pathlib import Path
from xml.etree import ElementTree

import pytest

from manyselves.core.reporting.agentic_models import TaskEnvelope
from manyselves.core.reporting.config import AgentDefinition, load_agent_definition
from manyselves.core.reporting.module_skills import ModuleSkillLibrary
from manyselves.core.reporting.prompts import PromptAssembler


def test_system_prompt_contains_identity_without_project_runtime_paths(tmp_path: Path) -> None:
    identity = tmp_path / "agent.md"
    identity.write_text(
        "---\nname: auditor\ndescription: 审计员\ntools: [submit_result]\nmaxTurns: 4\n---\n"
        "<role_and_perspective>独立核验。</role_and_perspective>",
        encoding="utf-8",
    )

    prompt = PromptAssembler.system_prompt(load_agent_definition(identity))

    assert "独立核验" in prompt
    assert str(tmp_path) not in prompt


def test_system_prompt_quotes_identity_name_as_one_xml_attribute(tmp_path: Path) -> None:
    identity = tmp_path / "agent.md"
    identity.write_text(
        "---\nname: 'auditor\" injected=\"yes'\ndescription: 审计员\n---\n"
        "<role_and_perspective>独立核验。</role_and_perspective>",
        encoding="utf-8",
    )

    root = ElementTree.fromstring(PromptAssembler.system_prompt(load_agent_definition(identity)))

    assert root.attrib == {"name": 'auditor" injected="yes'}


def test_system_prompt_injects_escaped_module_skill_content(tmp_path: Path) -> None:
    definition = AgentDefinition(
        name="module-2.1-specialist",
        description="架构专家",
        instructions="<role>分析系统。</role>",
        source_path=tmp_path / "agent.md",
    )
    skill = ModuleSkillLibrary.packaged().for_agent(definition.id)[0]

    root = ElementTree.fromstring(PromptAssembler.system_prompt(definition, module_skills=[skill]))

    skill_node = root.find("./module_skills/module_skill")
    assert skill_node is not None
    assert skill_node.attrib["id"] == "pds.module21.architecture"
    assert skill_node.attrib["module_id"] == "2.1"
    assert "负荷率必须保留计算口径" in (skill_node.text or "")


def test_planner_prompt_contains_skill_index_but_not_bodies(tmp_path: Path) -> None:
    definition = AgentDefinition(
        name="report-planner",
        description="规划负责人",
        instructions="<role>规划模块。</role>",
        source_path=tmp_path / "agent.md",
    )
    library = ModuleSkillLibrary.packaged()

    prompt = PromptAssembler.system_prompt(definition, module_skill_index=library.index_text())

    assert "pds.module24.configuration" in prompt
    assert "负荷率必须保留计算口径" not in prompt


def test_system_prompt_rejects_malformed_identity_xml(tmp_path: Path) -> None:
    identity = tmp_path / "agent.md"
    identity.write_text(
        "---\nname: auditor\ndescription: 审计员\n---\n<role>AT&T</role>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="valid XML fragment"):
        PromptAssembler.system_prompt(load_agent_definition(identity))


@pytest.mark.parametrize("field_name", ["name", "description"])
def test_system_prompt_rejects_xml_forbidden_identity_metadata(
    tmp_path: Path,
    field_name: str,
) -> None:
    values = {"name": "auditor", "description": "审计员"}
    values[field_name] += "\x01"
    definition = AgentDefinition(
        **values,
        instructions="<role_and_perspective>独立核验。</role_and_perspective>",
        source_path=tmp_path / "agent.md",
    )

    with pytest.raises(ValueError, match="system prompt must be valid XML"):
        PromptAssembler.system_prompt(definition)


def test_task_context_is_xml_and_separate_from_system() -> None:
    envelope = TaskEnvelope(
        task_id="t1",
        run_id="r1",
        agent_id="auditor",
        objective="审 2.4 & 核验",
        input_refs=["evidence/<2.4>.json"],
        constraints=["不得编造 & 必须引用"],
        allowed_outputs=["module_submission"],
        prior_result_ref="drafts/2.4-v0.json",
        issue_refs=["issues/2.4-1.json"],
    )

    message = PromptAssembler.task_message(envelope, ["drafts/2.4.json"])

    assert message.startswith("<task_context>")
    root = ElementTree.fromstring(message)
    assert root.findtext("agent_id") == "auditor"
    assert root.findtext("objective") == "审 2.4 & 核验"
    assert root.findtext("input_ref") == "evidence/<2.4>.json"
    assert root.findtext("shared_artifact") == "drafts/2.4.json"
    assert root.findtext("constraint") == "不得编造 & 必须引用"
    assert root.findtext("allowed_output") == "module_submission"
    assert root.findtext("prior_result_ref") == "drafts/2.4-v0.json"
    assert root.findtext("issue_ref") == "issues/2.4-1.json"


def test_task_context_marks_session_summary_as_context_only() -> None:
    envelope = TaskEnvelope(
        task_id="t-summary",
        run_id="run-summary",
        agent_id="module-2.4-specialist",
        objective="修订设备模块",
        context_summary_refs=["Work/report-versions/v1/session-summaries/summary.json"],
    )

    message = PromptAssembler.task_message(envelope, [])

    assert 'context_only="true"' in message
    assert "session-summaries/summary.json" in message


def test_task_context_rejects_xml_forbidden_envelope_value() -> None:
    envelope = TaskEnvelope(
        task_id="t1",
        run_id="r1",
        agent_id="auditor",
        objective="审计\x01 2.4",
    )

    with pytest.raises(ValueError, match="task context must be valid XML"):
        PromptAssembler.task_message(envelope, [])


def test_task_context_rejects_xml_forbidden_shared_artifact() -> None:
    envelope = TaskEnvelope(
        task_id="t1",
        run_id="r1",
        agent_id="auditor",
        objective="审计 2.4",
    )

    with pytest.raises(ValueError, match="task context must be valid XML"):
        PromptAssembler.task_message(envelope, ["drafts/2.4\x01.json"])
