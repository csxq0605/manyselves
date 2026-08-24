"""Generic Host characterization for one file-defined Cross owner pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_composition import (
    build_cross_owner_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_definitions import (
    register_cross_owner_pipeline_specializations,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossOwnerFindingSubmission,
    CrossOwnerVerdictSubmission,
    CrossReviewCoverageEntry,
    CrossReviewFinding,
    ModuleReviewCoverage,
    ModuleReviewFinding,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
)
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerInitialAgentResult,
    DeclarativeCrossOwnerRecheckAgentResult,
    DeclarativeMainExceptionUserInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRecheckAgentResult,
    DeclarativeModuleReviewAgentResult,
    DeclarativeModuleRevisionAgentResult,
)
from manyselves.kernel.conversations import ConversationRegistry
from manyselves.kernel.definitions import AgentDefinition, DefinitionKind, TaskDefinition
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.state_store import InMemoryWorkflowStateStore
from manyselves.runtime.workflow_host import (
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)

_OWNER = "2.1"
_DIMENSIONS = [
    "terminology",
    "facts",
    "risk_levels",
    "dependencies",
    "propagation",
    "joint_verification",
]


def test_main_exception_input_schema_carries_generic_ui_labels() -> None:
    schema = DeclarativeMainExceptionUserInput.model_json_schema()

    assert schema["properties"]["decision"]["title"] == "处理方式"
    assert schema["properties"]["decision"]["x-enum-labels"] == {
        "accept_dispute": "接受争议并继续",
        "return_to_author": "退回作者修改",
        "stop_incomplete": "停止并保留不完整结果",
    }
    assert schema["properties"]["rationale"]["title"] == "说明"


class _TypedCrossScript:
    """Scripted Agent ports returning the actual Capability result wrappers."""

    def __init__(self, run_id: str, *, local_finding: bool = False) -> None:
        self.run_id = run_id
        self.local_finding = local_finding
        self.calls: list[tuple[str, str, str, Any]] = []
        self.task_calls: dict[str, int] = {}

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        del value
        occurrence = self.task_calls.get(task.id, 0) + 1
        self.task_calls[task.id] = occurrence
        result = self._result(task.id, occurrence=occurrence)
        self.calls.append((agent.id, task.id, conversation.conversation_id, result))
        return AgentInvocationOutcome(
            status="ok",
            result=result,
            session_id=f"script:{conversation.key.value}",
        )

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
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

    def _result(self, task_id: str, *, occurrence: int = 1) -> Any:
        coverage = CrossReviewCoverageEntry(
            module_id=_OWNER,
            checked_dimensions=_DIMENSIONS,
        )
        if task_id == "cross-owner-runtime-initial-review":
            finding = CrossReviewFinding(
                id="X-2.1-r0-1",
                owner_module_id=_OWNER,
                target_submodule_ids=["2.1.1"],
                related_module_ids=["2.2"],
                category="dependencies",
                impact="blocking",
                observation=(
                    "当前模块没有把该关系写入可验证正文，导致关联模块无法复核其依赖边界。"
                ),
                evidence_refs=[f"Work/runs/{self.run_id}/modules/2.1-r0.json"],
                required_change=(
                    "在 2.1.1 中补充关系机制、影响路径和与 2.2 的联合验证步骤，"
                    "并明确后续复核方法。"
                ),
                reviewer_checks=["确认关系机制已写入目标小节并能由关联模块复核。"],
            )
            return DeclarativeCrossOwnerInitialAgentResult(
                status="completed",
                submission=CrossOwnerFindingSubmission(
                    owner_module_id=_OWNER,
                    coverage=coverage,
                    findings=[finding],
                ),
            )
        if task_id == "cross-owner-module-2.1-revision-r1" and (
            occurrence == 1 or not self.local_finding
        ):
            return DeclarativeModuleRevisionAgentResult(
                status="completed",
                submission=ModuleRevisionSubmission(
                    module_id=_OWNER,
                    base_revision=0,
                    revision=1,
                    submodule_narratives={
                        "2.1.1": "补充关系机制、影响路径与和 2.2 的联合验证步骤。"
                    },
                    source_ids=[],
                    unresolved_questions=[],
                    revision_responses=[
                        RevisionResponse(
                            finding_id="X-2.1-r0-1",
                            action="implemented",
                            summary="已补充关系机制及联合验证步骤，供后续复核。",
                            changed_target_ids=["2.1.1"],
                        )
                    ],
                ),
            )
        if (
            task_id == "cross-owner-module-2.1-revision-r1"
            and occurrence == 2
            and self.local_finding
        ):
            return DeclarativeModuleRevisionAgentResult(
                status="completed",
                submission=ModuleRevisionSubmission(
                    module_id=_OWNER,
                    base_revision=1,
                    revision=2,
                    submodule_narratives={
                        "2.1.1": "进一步补充本地回归要求的联合验证步骤和边界说明。"
                    },
                    source_ids=[],
                    unresolved_questions=[],
                    revision_responses=[
                        RevisionResponse(
                            finding_id="M-2.1-cross-r1-r0-1",
                            action="implemented",
                            summary="已按本地 Auditor finding 补充联合验证步骤和回归边界说明。",
                            changed_target_ids=["2.1.1"],
                        )
                    ],
                ),
            )
        if task_id == "cross-owner-runtime-local-review":
            return DeclarativeModuleReviewAgentResult(
                status="completed",
                submission=ModuleReviewFindingSubmission(
                    coverage=ModuleReviewCoverage(submodule_ids=["2.1.1"]),
                    findings=(
                        [
                            ModuleReviewFinding(
                                id="M-2.1-cross-r1-r0-1",
                                target_submodule_id="2.1.1",
                                category="analysis_depth",
                                impact="blocking",
                                observation=(
                                    "本地回归发现修订内容仍未完整说明联合验证边界。"
                                ),
                                evidence_refs=[
                                    f"Work/runs/{self.run_id}/modules/2.1-r1.json"
                                ],
                                required_change=(
                                    "补充联合验证步骤和回归影响边界，供原 Auditor 复核。"
                                ),
                                reviewer_checks=[
                                    "确认联合验证步骤和回归边界已经写入目标小节。"
                                ],
                            )
                        ]
                        if self.local_finding
                        else []
                    ),
                ),
            )
        if task_id == "cross-owner-runtime-local-recheck" and self.local_finding:
            return DeclarativeModuleRecheckAgentResult(
                status="completed",
                submission=ModuleReviewVerdictSubmission(
                    coverage=ModuleReviewCoverage(submodule_ids=["2.1.1"]),
                    verdicts=[
                        ResolutionVerdict(
                            finding_id="M-2.1-cross-r1-r0-1",
                            verdict="resolved",
                            reason=(
                                "本地回归复核确认联合验证步骤和回归边界已经补充完成。"
                            ),
                            evidence_refs=[
                                f"Work/runs/{self.run_id}/modules/2.1-r2.json"
                            ],
                        )
                    ],
                ),
            )
        if task_id == "cross-owner-runtime-recheck":
            return DeclarativeCrossOwnerRecheckAgentResult(
                status="completed",
                submission=CrossOwnerVerdictSubmission(
                    owner_module_id=_OWNER,
                    coverage=coverage,
                    verdicts=[
                        ResolutionVerdict(
                            finding_id="X-2.1-r0-1",
                            verdict="resolved",
                            reason="修订后的关系机制和联合验证已经满足原 finding 的检查要求。",
                            evidence_refs=[
                                f"Work/runs/{self.run_id}/modules/2.1-r1.json"
                            ],
                        )
                    ],
                ),
            )
        raise AssertionError(f"unexpected scripted Cross task: {task_id}")


def _module_submission(module_id: str) -> ModuleSubmission:
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"初始内容 {submodule_id}。"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )


def _reporting_state(workspace: Path, run_id: str) -> dict[str, Any]:
    modules = {
        module_id: _module_submission(module_id) for module_id in REPORT_TAXONOMY
    }
    module_refs = {}
    for module_id, submission in modules.items():
        ref = f"Work/runs/{run_id}/modules/{module_id}-r0.json"
        module_refs[module_id] = {"ref": ref, "sha256": "0" * 64}
        path = workspace / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(submission.model_dump_json(), encoding="utf-8")
    completion_ref = (
        f"Work/runs/{run_id}/reviews/module/initial/2.1/completion-r0.json"
    )
    completion = ReviewCompletionRecord(
        lifecycle="module",
        run_id=run_id,
        reviewer_agent_id="evidence-auditor",
        reviewer_session_key="module-auditor-2.1",
        subject_refs=[module_refs[_OWNER]["ref"]],
        finding_refs=[],
        verdict_refs=[],
        resolved_finding_ids=[],
    )
    completion_path = workspace / completion_ref
    completion_path.parent.mkdir(parents=True, exist_ok=True)
    completion_path.write_text(completion.model_dump_json(), encoding="utf-8")
    return {
        "run_id": run_id,
        "workflow_id": "distribution-cross-owner-2.1-pipeline",
        "module_submissions": modules,
        "module_artifact_refs": module_refs,
        "module_review_completion_refs": {_OWNER: completion_ref},
    }


@pytest.mark.asyncio
async def test_cross_owner_file_workflow_host_finding_revision_local_recheck_completes(
    tmp_path: Path,
) -> None:
    """Run one packaged owner pipeline through the real generic Host."""

    run_id = "cross-owner-workflow-host"
    _capability, registry = load_distribution_reporting_capability()
    register_cross_owner_pipeline_specializations(registry)
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )
    runtime = CrossOwnerRuntime(tmp_path)
    script = _TypedCrossScript(run_id)
    context = RuntimeContext(
        tools=build_cross_owner_tool_implementations(runtime),
        agents={
            "cross-module-reviewer": script,
            "module-2.1-specialist": script,
            "evidence-auditor": script,
        },
        conversations=ConversationRegistry(),
    )
    state = WorkflowState.for_plan(
        run_id,
        plan,
        initial_variables={"reporting-state": _reporting_state(tmp_path, run_id)},
    )
    state_store = InMemoryWorkflowStateStore()
    final = await WorkflowRuntimeHost(
        build_builtin_executor_registry(),
        state_store,
        InMemoryWorkflowEventSink(),
    ).execute(plan, state, context)

    assert final.status == "completed"
    outcome = final.outputs["result"]
    assert outcome.status == "completed"
    assert outcome.pipeline["initial_result_ref"].endswith(
        "cross-owner-findings-r0-2.1.json"
    )
    assert outcome.pipeline["verdict_ref"].endswith(
        "cross-owner-verdicts-r1-2.1.json"
    )
    assert [task_id for _agent, task_id, _conversation, _result in script.calls] == [
        "cross-owner-runtime-initial-review",
        "cross-owner-module-2.1-revision-r1",
        "cross-owner-runtime-local-review",
        "cross-owner-runtime-recheck",
    ]
    reviewer_calls = [
        conversation_id
        for agent_id, _task_id, conversation_id, _result in script.calls
        if agent_id == "cross-module-reviewer"
    ]
    assert reviewer_calls == [
        "run:cross-owner-workflow-host:cross-module-reviewer:cross-owner-2.1",
        "run:cross-owner-workflow-host:cross-module-reviewer:cross-owner-2.1",
    ]
    assert reviewer_calls[0] == reviewer_calls[1]


@pytest.mark.asyncio
async def test_cross_owner_file_workflow_host_local_finding_revision_recheck_completes(
    tmp_path: Path,
) -> None:
    """Exercise the nested local Module revision/recheck lifecycle via Generic Host."""

    run_id = "cross-owner-workflow-host-local-finding"
    _capability, registry = load_distribution_reporting_capability()
    register_cross_owner_pipeline_specializations(registry)
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )
    runtime = CrossOwnerRuntime(tmp_path)
    script = _TypedCrossScript(run_id, local_finding=True)
    context = RuntimeContext(
        tools=build_cross_owner_tool_implementations(runtime),
        agents={
            "cross-module-reviewer": script,
            "module-2.1-specialist": script,
            "evidence-auditor": script,
        },
        conversations=ConversationRegistry(),
    )
    state = WorkflowState.for_plan(
        run_id,
        plan,
        initial_variables={"reporting-state": _reporting_state(tmp_path, run_id)},
    )
    final = await WorkflowRuntimeHost(
        build_builtin_executor_registry(),
        InMemoryWorkflowStateStore(),
        InMemoryWorkflowEventSink(),
    ).execute(plan, state, context)

    assert final.status == "completed"
    outcome = final.outputs["result"]
    assert outcome.status == "completed"
    assert [task_id for _agent, task_id, _conversation, _result in script.calls] == [
        "cross-owner-runtime-initial-review",
        "cross-owner-module-2.1-revision-r1",
        "cross-owner-runtime-local-review",
        "cross-owner-module-2.1-revision-r1",
        "cross-owner-runtime-local-recheck",
        "cross-owner-runtime-recheck",
    ]
    assert script.task_calls["cross-owner-module-2.1-revision-r1"] == 2
    module_revision_conversations = [
        conversation
        for agent_id, task_id, conversation, _result in script.calls
        if agent_id == "module-2.1-specialist"
        and task_id == "cross-owner-module-2.1-revision-r1"
    ]
    assert len(module_revision_conversations) == 2
    assert module_revision_conversations[0] == module_revision_conversations[1]

    local_auditor_conversations = [
        conversation
        for agent_id, task_id, conversation, _result in script.calls
        if agent_id == "evidence-auditor"
        and task_id
        in {
            "cross-owner-runtime-local-review",
            "cross-owner-runtime-local-recheck",
        }
    ]
    assert len(local_auditor_conversations) == 2
    assert local_auditor_conversations[0] == local_auditor_conversations[1]

    cross_reviewer_conversations = [
        conversation
        for agent_id, _task_id, conversation, _result in script.calls
        if agent_id == "cross-module-reviewer"
    ]
    assert len(cross_reviewer_conversations) == 2
    assert cross_reviewer_conversations[0] == cross_reviewer_conversations[1]
