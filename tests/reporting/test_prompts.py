from pathlib import Path
from xml.etree import ElementTree

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
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


def test_system_prompt_makes_declared_reasoning_effort_effective(tmp_path: Path) -> None:
    definition = AgentDefinition(
        name="module-2.1-specialist",
        description="架构专家",
        effort="high",
        instructions="<role>分析系统。</role>",
        source_path=tmp_path / "agent.md",
    )

    root = ElementTree.fromstring(PromptAssembler.system_prompt(definition))

    profile = root.find("execution_profile")
    assert profile is not None
    assert profile.attrib == {"effort": "high", "model": "inherit"}
    assert "alternative explanations" in (profile.text or "")


def test_routing_metadata_can_be_injected_without_skill_bodies(tmp_path: Path) -> None:
    definition = AgentDefinition(
        name="main-agent",
        description="项目负责人",
        instructions="<role>分配模块。</role>",
        source_path=tmp_path / "agent.md",
    )
    library = ModuleSkillLibrary.packaged()

    prompt = PromptAssembler.system_prompt(definition, module_skill_index=library.index_text())

    assert "pds.module24.configuration" in prompt
    assert "负荷率必须保留计算口径" not in prompt


def test_packaged_main_prompt_is_scoped_to_workflow_exceptions() -> None:
    definition = load_agent_definition(
        Path(__file__).parents[2] / "manyselves/templates/reporting/agents/main-agent.md"
    )

    prompt = PromptAssembler.system_prompt(definition)

    assert "例外裁决者" in prompt
    assert "只处理当前 workflow_exception_input" in prompt
    assert "直接模块分配" not in prompt


def test_packaged_template_distiller_requires_real_template_skill_submission() -> None:
    definition = load_agent_definition(
        Path(__file__).parents[2]
        / "manyselves/templates/reporting/agents/template-distiller.md"
    )

    prompt = PromptAssembler.system_prompt(definition)

    assert "只迁移去事实化的方法" in prompt
    assert "项目事实" in prompt
    assert "required_part_ids" in prompt
    assert "十四份" not in prompt
    assert "禁止生成 Cross Skill" not in prompt
    assert "不链接共享 reference" not in prompt


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
    submission = root.find("submission_contract")
    assert submission is not None
    assert submission.attrib == {
        "kind": "module_submission",
        "version": "1",
        "machine_schema": "submit_result.input_schema",
    }


def test_task_context_inlines_input_and_keeps_only_compact_submission_semantics() -> None:
    envelope = TaskEnvelope(
        task_id="audit-2.1",
        run_id="run-audit",
        agent_id="evidence-auditor",
        objective="审查模块 2.1",
        allowed_outputs=["module_review_finding_submission"],
        input_refs=["Work/runs/run-audit/reviews/input.json"],
        input_contract_kind="module_review_input",
        input_contract_ref="Work/runs/run-audit/reviews/input.json",
    )
    payload = '{"kind":"module_review_input","subject":{"module_id":"2.1"}}'

    root = ElementTree.fromstring(
        PromptAssembler.task_message(
            envelope,
            [],
            input_contract_payload=payload,
        )
    )

    assert root.findtext("input_contract") == payload
    submission = root.find("submission_contract")
    assert submission is not None
    assert submission.attrib["kind"] == "module_review_finding_submission"
    assert submission.attrib["version"] == "1"
    assert submission.attrib["machine_schema"] == "submit_result.input_schema"
    assert submission.findtext("purpose")
    assert submission.findtext("semantic_rule")
    argument_encoding = submission.findtext("argument_encoding") or ""
    assert "tool arguments are the submission object itself" in argument_encoding
    assert "Never add a payload wrapper" in argument_encoding
    rendered = ElementTree.tostring(submission, encoding="unicode")
    assert "valid_example" not in rendered
    assert "top_level_fields" not in rendered
    assert "nested_type" not in rendered


def test_task_context_declares_artifact_delivery_modes() -> None:
    contract_ref = "Work/runs/run-audit/reviews/input.json"
    prior_ref = "Work/runs/run-audit/modules/2.1-r0.json"
    summary_ref = "Work/runs/run-audit/context/summary.json"
    envelope = TaskEnvelope(
        task_id="audit-2.1-r1",
        run_id="run-audit",
        agent_id="evidence-auditor",
        objective="复审模块 2.1",
        input_refs=[contract_ref],
        prior_result_ref=prior_ref,
        context_summary_refs=[summary_ref],
        artifact_delivery_modes={
            contract_ref: "inline",
            prior_ref: "hash_retained",
            summary_ref: "reference",
        },
        input_contract_kind="module_review_input",
        input_contract_ref=contract_ref,
    )

    root = ElementTree.fromstring(
        PromptAssembler.task_message(
            envelope,
            [],
            input_contract_payload='{"kind":"module_review_input"}',
        )
    )

    assert root.find("input_ref").attrib["delivery_mode"] == "inline"
    assert root.find("input_contract").attrib["delivery_mode"] == "inline"
    assert root.find("prior_result_ref").attrib["delivery_mode"] == "hash_retained"
    assert root.find("context_summary_ref").attrib["delivery_mode"] == "reference"


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


def test_task_context_inlines_bounded_workflow_context() -> None:
    envelope = TaskEnvelope(
        task_id="audit-2.4-r0",
        run_id="run-audit",
        agent_id="evidence-auditor",
        objective="审计 2.4",
        inline_context='{"fact":"A&B"}',
    )

    root = ElementTree.fromstring(PromptAssembler.task_message(envelope, []))

    assert root.findtext("inline_context") == '{"fact":"A&B"}'


def test_task_context_does_not_expose_internal_module_dispatch() -> None:
    envelope = TaskEnvelope(
        task_id="module-2.1",
        run_id="run-module",
        agent_id="module-2.1-specialist",
        objective="完成模块 2.1",
    )

    message = PromptAssembler.task_message(envelope, [])

    assert "expected_plan_agent_id" not in message
    assert "plan_submission" not in message


def test_task_context_does_not_duplicate_declared_refs_as_shared_artifacts() -> None:
    envelope = TaskEnvelope(
        task_id="cross-module-review",
        run_id="run-review",
        agent_id="cross-module-reviewer",
        objective="审查",
        input_refs=["Work/review.json", "Work/issues.json"],
        prior_result_ref="Work/prior.json",
    )

    root = ElementTree.fromstring(
        PromptAssembler.task_message(
            envelope,
            [
                "Work/review.json",
                "Work/issues.json",
                "Work/prior.json",
                "Work/extra.json",
                "Work/extra.json",
            ],
        )
    )

    assert [node.text for node in root.findall("shared_artifact")] == [
        "Work/extra.json"
    ]


def test_task_context_lists_every_fixed_target_submodule() -> None:
    envelope = TaskEnvelope(
        task_id="module-2.4",
        run_id="run-module",
        agent_id="module-2.4-specialist",
        objective="完成设备模块",
        target_submodule_ids=["2.4.1.1", "2.4.1.2", "2.4.1.3"],
    )

    root = ElementTree.fromstring(PromptAssembler.task_message(envelope, []))

    assert [node.text for node in root.findall("target_submodule_id")] == [
        "2.4.1.1",
        "2.4.1.2",
        "2.4.1.3",
    ]


def test_task_context_lists_recovery_tool_boundary() -> None:
    envelope = TaskEnvelope(
        task_id="module-2.4",
        run_id="run-module",
        agent_id="module-2.4-specialist",
        objective="恢复设备模块",
        allowed_tools=["list_result_parts", "write_result_part", "submit_result"],
    )

    root = ElementTree.fromstring(PromptAssembler.task_message(envelope, []))

    assert [node.text for node in root.findall("allowed_tool")] == [
        "list_result_parts",
        "write_result_part",
        "submit_result",
    ]


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


def test_task_delta_contains_only_changed_business_fields() -> None:
    previous = {
        "task_id": "module-2.1-r0",
        "revision": 0,
        "objective": "完成初稿",
        "constraints": ["引用证据"],
        "allowed_outputs": ["module_submission"],
        "allowed_tools": ["submit_result"],
        "input_refs": ["Work/input.json"],
        "prior_result_ref": None,
        "context_summary_refs": [],
        "inline_context": None,
        "target_submodule_ids": ["2.1.1"],
        "artifact_delivery_modes": {"Work/input.json": "inline"},
        "input_contract_kind": "module_authoring_input",
        "input_contract_ref": "Work/input.json",
        "input_contract_payload": '{"kind":"module_authoring_input","revision":0}',
        "shared_artifacts": ["Work/input.json"],
    }
    envelope = TaskEnvelope(
        task_id="module-2.1-r1",
        run_id="run-delta",
        agent_id="module-2.1-specialist",
        objective="完成定向修订",
        input_refs=["Work/input.json", "Work/finding.json"],
        artifact_delivery_modes={
            "Work/input.json": "inline",
            "Work/finding.json": "reference",
        },
        constraints=["引用证据"],
        allowed_outputs=["module_submission"],
        allowed_tools=["submit_result"],
        revision=1,
        target_submodule_ids=["2.1.1"],
        input_contract_kind="module_authoring_input",
        input_contract_ref="Work/input.json",
    )
    message = PromptAssembler.task_delta_message(
        envelope,
        ["Work/input.json", "Work/finding.json"],
        previous=previous,
        input_contract_payload='{"kind":"module_authoring_input","revision":0}',
    )
    root = ElementTree.fromstring(message)
    assert root.tag == "task_boundary"
    delta = root.find("task_delta")
    assert delta is not None
    assert delta.attrib["changed_fields"] == "objective,input_refs,artifact_delivery_modes"
    assert delta.findtext("objective") == "完成定向修订"
    assert [node.text for node in delta.findall("input_ref")] == [
        "Work/input.json",
        "Work/finding.json",
    ]
    assert delta.find("allowed_output") is None
    assert delta.find("input_contract") is None
    assert delta.find("input_contract_ref").attrib["unchanged"] == "true"
    assert delta.findtext("new_shared_artifact") == "Work/finding.json"
    assert "Historical calls are transcript history only" in message
