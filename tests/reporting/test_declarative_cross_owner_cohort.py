from types import SimpleNamespace

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.core.reporting.agentic_models import (
    CrossOwnerFindingSubmission,
    CrossReviewCoverageEntry,
    TaskEnvelope,
)
from manyselves.core.reporting.declarative_cross_owner_cohort import (
    DeclarativeCrossOwnerRuntimeContext,
    _CrossOwnerReviewerInvoker,
)
from manyselves.core.reporting.review_lifecycle import (
    CrossOwnerInitialReviewPreparation,
)
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationMode,
    ConversationRecord,
)
from manyselves.kernel.definitions import DefinitionKind


@pytest.mark.asyncio
async def test_cross_reviewer_wrapper_forwards_declared_recovery_policy_and_session() -> None:
    _capability, definitions = load_distribution_reporting_capability()
    agent = definitions.require(DefinitionKind.AGENT, "cross-module-reviewer")
    task = definitions.require(
        DefinitionKind.TASK,
        "cross-owner-runtime-initial-review",
    )
    recovery = definitions.require(
        DefinitionKind.RECOVERY,
        "current-reporting-recovery",
    )
    envelope = TaskEnvelope(
        task_id=task.id,
        run_id="run-cross-wrapper-recovery",
        agent_id=agent.id,
        objective=task.objective,
        allowed_outputs=["cross_owner_finding_submission"],
    )
    preparation = CrossOwnerInitialReviewPreparation.model_construct(
        mode="invoke_agent",
        run_id=envelope.run_id,
        workflow_id="workflow-cross-wrapper-recovery",
        owner_module_id="2.1",
        review_round=0,
        reviewer_session_key="cross-owner-2.1",
        owner_input_ref="Work/runs/run-cross-wrapper-recovery/context/cross-2.1.json",
        envelope=envelope,
    )
    context = DeclarativeCrossOwnerRuntimeContext(
        owner_module_id="2.1",
        status="initial_ready",
        preparation=preparation,
    )

    class SpyRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def _agent(self, agent_id, envelope, artifacts, workflow_id, **kwargs):
            self.calls.append(
                {
                    "agent_id": agent_id,
                    "envelope": envelope,
                    "artifacts": artifacts,
                    "workflow_id": workflow_id,
                    **kwargs,
                }
            )
            return CrossOwnerFindingSubmission(
                owner_module_id="2.1",
                coverage=CrossReviewCoverageEntry(
                    module_id="2.1",
                    checked_dimensions=["facts"],
                ),
            )

    spy_runner = SpyRunner()
    invoker = _CrossOwnerReviewerInvoker(
        SimpleNamespace(
            _current_runner=spy_runner,
            _workflow_id="workflow-cross-wrapper-recovery",
        )
    )
    conversation = ConversationRecord(
        conversation_id="run-cross-wrapper-recovery:cross-owner-2.1",
        key=ConversationKey(
            agent_id=agent.id,
            value="cross-owner-2.1",
            mode=ConversationMode.RUN,
        ),
        run_id=envelope.run_id,
    )

    outcome = await invoker.invoke_with_recovery(
        agent,
        task,
        context,
        conversation,
        task_id=task.id,
        recovery_policy=recovery,
    )

    assert outcome.status == "ok"
    assert len(spy_runner.calls) == 1
    assert spy_runner.calls[0]["agent_id"] == agent.id
    assert spy_runner.calls[0]["workflow_id"] == preparation.workflow_id
    assert spy_runner.calls[0]["session_key"] == "cross-owner-2.1"
    assert spy_runner.calls[0]["recovery_policy"] is recovery
