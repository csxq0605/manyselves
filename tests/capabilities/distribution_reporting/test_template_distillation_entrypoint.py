"""Characterization for the file-defined template Skill distillation entrypoint."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TEMPLATE_ROLE_SKILL_IDS,
    TEMPLATE_SKILL_EXCLUSION_CATEGORIES,
    TEMPLATE_SKILL_TRANSFER_CATEGORIES,
    TemplateSkillBoundaryManifest,
    TemplateSkillSubmission,
)
from manyselves.kernel.definitions import DefinitionKind


def test_distill_template_skill_compiles_with_typed_agent_contracts() -> None:
    """The file definition now resolves through the generic compiler."""

    from manyselves.kernel.executors import build_builtin_executor_registry
    from manyselves.kernel.workflow import WorkflowCompiler

    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(DefinitionKind.WORKFLOW, "distill-template-skill")

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)

    assert plan.workflow_id == "distill-template-skill"
    assert plan.tool_ids == [
        "prepare-template-distillation",
        "materialize-template-skill",
    ]


@pytest.mark.asyncio
async def test_distill_template_skill_host_entrypoint_materializes_typed_agent_output(
    tmp_path: Path,
) -> None:
    """The file workflow must execute through the Generic Host once bound."""

    from manyselves.kernel.ports import AgentInvocationOutcome

    class RecordingAgentInvoker:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def invoke(
            self,
            agent,
            task,
            value,
            conversation,
            *,
            task_id: str,
        ) -> AgentInvocationOutcome:
            self.calls.append(
                {
                    "agent": agent.id,
                    "task": task.id,
                    "value": value,
                    "conversation_id": conversation.conversation_id,
                    "session_key": conversation.key.value,
                    "task_id": task_id,
                }
            )
            return AgentInvocationOutcome(
                status="ok",
                result=_submission().model_dump(mode="json"),
                session_id="template-distiller-session",
            )

        async def invoke_with_recovery(
            self,
            agent,
            task,
            value,
            conversation,
            *,
            task_id: str,
            recovery_policy,
        ) -> AgentInvocationOutcome:
            del recovery_policy
            return await self.invoke(
                agent,
                task,
                value,
                conversation,
                task_id=task_id,
            )

    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        TemplateDistillationInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.template_distillation import (
        TemplateDistillationWorkflowRuntime,
    )

    template = tmp_path / "Templates/report_template.docx"
    template.parent.mkdir(parents=True)
    template.write_bytes(b"template source")
    run_id = "distill-template-host"
    request = TemplateDistillationInput(
        run_id=run_id,
        template_ref="Templates/report_template.docx",
        inspect_max_chars=100_000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    invoker = RecordingAgentInvoker()

    runtime = TemplateDistillationWorkflowRuntime(
        tmp_path,
        agent_invoker=invoker,
    )
    result = await runtime.start(
        uuid4(),
        "distill-template-skill",
        request.model_dump(mode="json"),
    )

    assert result["run_id"] == run_id
    assert len(invoker.calls) == 1
    call = invoker.calls[0]
    assert call["agent"] == "template-distiller"
    assert call["task"] == "template-skill-distillation"
    assert call["session_key"] == "template-distillation"
    assert call["value"].template_ref == (
        f"Work/runs/{run_id}/templates/template-for-skill.docx"
    )
    assert (
        tmp_path / f"Work/runs/{run_id}/context/template-distillation-input.json"
    ).is_file()
    assert (
        tmp_path / f"Work/runs/{run_id}/templates/template-for-skill.docx"
    ).is_symlink()
    run = runtime.get_run(result["run_id"])
    assert run["run"]["status"] == "completed"
    outputs = runtime.get_outputs(result["run_id"])
    materialization = next(
        item["value"] for item in outputs["outputs"] if item["id"] == "result"
    )
    assert materialization["kind"] == "template_skill_materialization"
    assert len(materialization["skill_refs"]) == len(TEMPLATE_ROLE_SKILL_IDS)
    assert all((tmp_path / ref).is_file() for ref in materialization["skill_refs"])


@pytest.mark.asyncio
async def test_typed_distillation_agent_invoker_keeps_runner_session_and_contract_refs() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        TemplateDistillationInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.template_distillation import (
        TemplateDistillationAgentInvoker,
    )
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import AgentDefinition, TaskDefinition

    class RecordingRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def run(self, definition, envelope, artifacts, **kwargs):
            self.calls.append(
                {
                    "definition": definition,
                    "envelope": envelope,
                    "artifacts": artifacts,
                    "kwargs": kwargs,
                }
            )
            return SimpleNamespace(
                status="completed",
                payload=_submission(),
                session_id="provider-template-session",
            )

    agent = AgentDefinition(
        id="template-distiller",
        version="1.0.0",
        description="typed template distiller",
        instructions="distill",
        accepts=["template_distillation_input"],
        produces=["template_skill_submission"],
    )
    task = TaskDefinition(
        id="template-skill-distillation",
        version="1.0.0",
        description="distill task",
        agent=agent.id,
        objective="distill one template",
        input_contract="template_distillation_input",
        output_contract="template_skill_submission",
        tools=[
            "inspect_document",
            "write_result_part",
            "list_result_parts",
            "submit_result",
            "report_blocked",
        ],
    )
    value = TemplateDistillationInput(
        run_id="run-template-invoker",
        template_ref="Work/runs/run-template-invoker/templates/template-for-skill.docx",
        inspect_max_chars=100_000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(
            agent_id=agent.id,
            value="template-distillation",
            mode="run",
        ),
        run_id=value.run_id,
    )
    runner = RecordingRunner()
    invoker = TemplateDistillationAgentInvoker(
        runner,
        object(),
        workflow_id="distill-template-skill",
    )

    outcome = await invoker.invoke(
        agent,
        task,
        value,
        conversation,
        task_id="invoke-template-distiller",
    )

    assert outcome.status == "ok"
    assert outcome.session_id == "provider-template-session"
    assert conversation.external_session_id == "provider-template-session"
    assert len(runner.calls) == 1
    call = runner.calls[0]
    envelope = call["envelope"]
    assert envelope.input_contract_kind == "template_distillation_input"
    assert envelope.input_contract_ref in envelope.input_refs
    assert envelope.input_refs[-1] == value.template_ref
    assert call["kwargs"] == {
        "workflow_id": "distill-template-skill",
        "session_key": "template-distillation",
    }


def _submission() -> TemplateSkillSubmission:
    description = (
        "将模板中的证据限定、分析推进、综合表达、图证叙事与质量检查方法"
        "转化为可复用的角色职责指导。"
    )
    skills = {
        skill_id: (
            "---\n"
            f"name: report-template-{skill_id}\n"
            f"description: {description}\n"
            "---\n\n"
            f"# {skill_id} 模板方法\n\n"
            "先界定证据边界，再组织观察、判断、原因、影响和行动；"
            "使用去事实化结构样例检查表达是否可复用，并在提交前复核职责范围。\n"
            * 7
        )
        for skill_id in TEMPLATE_ROLE_SKILL_IDS
    }
    return TemplateSkillSubmission(
        skills=skills,
        boundary_manifest=TemplateSkillBoundaryManifest(
            transferred_categories=sorted(TEMPLATE_SKILL_TRANSFER_CATEGORIES),
            excluded_categories=sorted(TEMPLATE_SKILL_EXCLUSION_CATEGORIES),
            boundary_statement=(
                "只迁移可复用的分析、综合、图证和质量检查方法；项目事实、专业知识、"
                "标准阈值、客户身份、风险结论、建议以及证据编号必须来自当前运行时输入；"
                "Skill 不得自行补充任何项目判断，也不替代模块、Knowledge 或 Evidence 来源。"
            ),
        ),
    )


def test_template_distillation_plan_preserves_single_inspection_session_and_slices() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.template_distillation import (
        TEMPLATE_DISTILLATION_ALLOWED_TOOLS,
        TEMPLATE_DISTILLATION_SESSION_KEY,
        build_template_distillation_plan,
    )

    plan = build_template_distillation_plan(
        "run-template-entrypoint",
        "Work/runs/run-template-entrypoint/templates/template-for-skill.docx",
    )

    assert plan.task.task_id == "template-skill-distillation"
    assert plan.task.agent_id == "template-distiller"
    assert plan.task.run_id == "run-template-entrypoint"
    assert plan.session_key == TEMPLATE_DISTILLATION_SESSION_KEY == "template-distillation"
    assert plan.inspect_document.path == plan.input.template_ref
    assert plan.inspect_document.max_chars == 100_000
    assert plan.inspect_document.once is True
    assert plan.required_part_ids == TEMPLATE_ROLE_SKILL_IDS
    assert plan.allowed_tools == TEMPLATE_DISTILLATION_ALLOWED_TOOLS == (
        "inspect_document",
        "write_result_part",
        "list_result_parts",
        "submit_result",
        "report_blocked",
    )
    assert plan.continuation_part_ids(TEMPLATE_ROLE_SKILL_IDS[:6]) == (
        *TEMPLATE_ROLE_SKILL_IDS[6:],
    )
    assert plan.continuation_part_ids(TEMPLATE_ROLE_SKILL_IDS) == ()


@pytest.mark.asyncio
async def test_template_distillation_materializes_fourteen_skills_boundary_and_source(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.storage import (
        ReportingStore,
    )
    from manyselves.capabilities.distribution_reporting.runtime.template_distillation import (
        TemplateSkillMaterialization,
        build_template_distillation_tool_implementations,
    )
    from manyselves.kernel.contracts import build_contract_catalog
    from manyselves.kernel.definitions import ToolDefinition
    from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory

    store = ReportingStore(tmp_path)
    source_metadata = {
        "source": "packaged",
        "template_ref": "Work/runs/run-template-entrypoint/templates/template-for-skill.docx",
        "inspection_ref": (
            "Work/runs/run-template-entrypoint/context/template-inspection.json"
        ),
    }
    _capability, registry = load_distribution_reporting_capability()
    contracts = build_contract_catalog(registry)
    definition = registry.require(DefinitionKind.TOOL, "materialize-template-skill")
    assert isinstance(definition, ToolDefinition)
    adapter = CapabilityToolAdapterFactory(
        "distribution-reporting",
        build_template_distillation_tool_implementations(
            store,
            source_metadata=source_metadata,
        ),
        contracts,
    ).build(definition)
    outcome = await adapter.invoke(
        _submission(),
        task_id="run-template-entrypoint:distill-template-skill",
    )
    assert outcome.status == "ok"
    result = TemplateSkillMaterialization.model_validate(outcome.result)

    assert result.skill_refs == tuple(
        f"Work/report-template-role-skills/{skill_id}/SKILL.md"
        for skill_id in TEMPLATE_ROLE_SKILL_IDS
    )
    assert result.boundary_ref == "Work/report-template-role-skills/boundary.json"
    assert result.source_ref == "Work/report-template-role-skills/source.json"
    assert all((tmp_path / ref).is_file() for ref in result.skill_refs)

    boundary = json.loads(
        (tmp_path / "Work/report-template-role-skills/boundary.json").read_text(
            encoding="utf-8"
        )
    )
    source = json.loads(
        (tmp_path / "Work/report-template-role-skills/source.json").read_text(
            encoding="utf-8"
        )
    )
    assert boundary == _submission().boundary_manifest.model_dump(mode="json")
    assert source == {
        "source": "packaged",
        "template_ref": (
            "Work/runs/run-template-entrypoint/templates/template-for-skill.docx"
        ),
        "inspection_ref": (
            "Work/runs/run-template-entrypoint/context/template-inspection.json"
        ),
        "producer": "template-distiller",
        "task_id": "template-skill-distillation",
        "skill_root": "Work/report-template-role-skills",
        "boundary_policy_version": 1,
        "boundary_ref": "Work/report-template-role-skills/boundary.json",
    }


def test_distill_template_skill_definition_uses_generic_agent_actions() -> None:
    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(DefinitionKind.WORKFLOW, "distill-template-skill")
    task = registry.require(DefinitionKind.TASK, "template-skill-distillation")

    assert workflow.tasks == ["template-skill-distillation"]
    assert workflow.output_contract == "template_skill_materialization"
    assert [action["kind"] for action in workflow.actions] == [
        "invoke_tool",
        "create_conversation",
        "invoke_agent",
        "invoke_tool",
        "publish_result",
        "end_workflow",
    ]
    assert workflow.actions[0]["tool"] == "prepare-template-distillation"
    assert workflow.actions[0]["output_variable"] == "prepared-template-distillation-input"
    assert workflow.actions[1]["conversation_key"] == "template-distillation"
    assert workflow.actions[3]["tool"] == "materialize-template-skill"
    assert workflow.actions[3]["input_variable"] == "template-distillation-result"
    assert workflow.actions[4]["output"] == "template-skill-materialization"
    assert task.agent == "template-distiller"
    assert task.tools == [
        "inspect_document",
        "write_result_part",
        "list_result_parts",
        "submit_result",
        "report_blocked",
    ]
    assert task.input_contract == "template_distillation_input"
    assert task.output_contract == "template_skill_submission"
    agent = registry.require(DefinitionKind.AGENT, "template-distiller")
    assert agent.accepts == ["output_artifacts", "template_distillation_input"]
    assert agent.produces == ["output_artifacts", "template_skill_submission"]
    assert registry.require(
        DefinitionKind.TOOL,
        "prepare-template-distillation",
    ).model_visible is False
    assert registry.require(
        DefinitionKind.TOOL,
        "materialize-template-skill",
    ).model_visible is False
    assert registry.require(
        DefinitionKind.OUTPUT,
        "template-skill-materialization",
    ).contract == "template_skill_materialization"
