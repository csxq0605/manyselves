from pathlib import Path
from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleReviewFindingSubmission,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ModuleReviewInput,
    ModuleRevisionInput,
    ValidationReport,
)
from manyselves.core.reporting.config import load_packaged_agents
from manyselves.core.reporting.declarative_module_lane import (
    execute_declarative_module_lane,
)
from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
)
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.runtime.semantic_trace import SemanticTraceRecorder
from manyselves.runtime.state_store import FileWorkflowStateStore
from tests.reporting.test_legacy_semantic_trace import (
    _capture_legacy_module_lane,
    _module_lane_script,
)


class _ScriptedDeclarativeInvoker:
    def __init__(self, scripted: list[tuple[str, str, object]]) -> None:
        self._scripted = list(scripted)
        self.inputs: list[object] = []
        self.conversations: list[tuple[str, str]] = []
        self.recovery_policies: list[str] = []

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        expected_agent, expected_contract, result = self._scripted.pop(0)
        current = load_packaged_agents()[agent.id]
        assert agent.id == expected_agent
        assert task.output_contract == expected_contract
        assert agent.instructions == current.instructions
        assert agent.tools == current.tools
        self.inputs.append(value)
        self.conversations.append((agent.id, conversation.key.value))
        return AgentInvocationOutcome(status="ok", result=result)

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        self.recovery_policies.append(recovery_policy.id)
        return await self.invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
        )


def _passed_validation(
    run_id: str,
    subject: ModuleSubmission,
    subject_ref: str,
) -> ValidationReport:
    return ValidationReport(
        validation_protocol_version=2,
        run_id=run_id,
        subject_ref=subject_ref,
        subject_revision=subject.revision,
        validator="wp07-scripted-structural-validator-v1",
        check_ids=[],
        failures=[],
        passed=True,
    )


@pytest.mark.asyncio
async def test_declarative_module_lane_matches_legacy_semantic_trace(
    tmp_path: Path,
) -> None:
    run_id = "run-wp00-module-lane"
    workflow_id = "workflow-wp00-module-lane"
    module, target, scripted = _module_lane_script(run_id)
    invoker = _ScriptedDeclarativeInvoker(scripted)
    trace = SemanticTraceRecorder()

    result = await execute_declarative_module_lane(
        run_id=run_id,
        workflow_id=workflow_id,
        module=module,
        initial_scope={target},
        lifecycle_id="initial",
        agent_invokers={
            "evidence-auditor": invoker,
            "module-2.1-specialist": invoker,
        },
        validate_subject=_passed_validation,
        state_store=FileWorkflowStateStore(tmp_path / "declarative"),
        trace=trace,
    )

    assert result.revision == 1
    assert trace.snapshot() == await _capture_legacy_module_lane(tmp_path / "legacy")
    assert [type(value) for value in invoker.inputs] == [
        ModuleReviewInput,
        ModuleRevisionInput,
        ModuleReviewInput,
    ]
    assert invoker.conversations == [
        ("evidence-auditor", "module-auditor-2.1"),
        ("module-2.1-specialist", "module-2.1"),
        ("evidence-auditor", "module-auditor-2.1"),
    ]
    assert invoker.recovery_policies == ["current-reporting-recovery"] * 3


@pytest.mark.asyncio
async def test_declarative_module_lane_skips_revision_without_findings(
    tmp_path: Path,
) -> None:
    run_id = "run-wp07-no-findings"
    module, target, _scripted = _module_lane_script(run_id)
    invoker = _ScriptedDeclarativeInvoker(
        [
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [target]},
                    findings=[],
                ),
            )
        ]
    )

    result = await execute_declarative_module_lane(
        run_id=run_id,
        workflow_id="workflow-wp07-no-findings",
        module=module,
        initial_scope={target},
        lifecycle_id="initial",
        agent_invokers={"evidence-auditor": invoker},
        validate_subject=_passed_validation,
        state_store=FileWorkflowStateStore(tmp_path),
    )

    assert result == module
    assert len(invoker.inputs) == 1
    assert isinstance(invoker.inputs[0], ModuleReviewInput)
    assert invoker.recovery_policies == ["current-reporting-recovery"]
    assert not invoker._scripted
