import json
import inspect
from types import SimpleNamespace

import pytest

from manyselves.core.reporting.workflow import (
    ReportWorkflowRunner,
    ReportingNeedsDecisionError,
    ReportingRunBudget,
)
from manyselves.core.usage_ledger import UsageLedger


def _record_attempt(tmp_path, run_id: str, *, tokens: int = 100) -> None:
    UsageLedger(tmp_path, run_id).record_attempt(
        run_id=run_id,
        task_id="module-2.1",
        agent_id="module-2.1-specialist",
        stage="module_authoring",
        phase="initial",
        status="success",
        input_tokens=tokens - 10,
        output_tokens=10,
        total_tokens=tokens,
        message_chars=500,
        tool_schema_chars=100,
    )


def _commit_boundary(
    budget: ReportingRunBudget,
    completed_stage: str,
    next_stage: str | None,
) -> None:
    prepared = budget.prepare_boundary(completed_stage, next_stage)
    budget.confirm_boundary_checkpoint(prepared["boundary_id"])


def test_pause_policy_stops_only_when_a_stage_boundary_is_evaluated(tmp_path) -> None:
    run_id = "run-cost-pause"
    budget = ReportingRunBudget(
        tmp_path,
        run_id,
        max_attempts=1,
        max_tokens=100,
        mode="pause_at_boundary",
    )

    _record_attempt(tmp_path, run_id)
    _commit_boundary(budget, "module-2.1", "module-2.2")

    with pytest.raises(ReportingNeedsDecisionError) as raised:
        budget.evaluate_boundary("module-2.1", "module-2.2")

    assert "checkpoint 已保存" in str(raised.value)
    state_path = tmp_path / f"Work/runs/{run_id}/cost-control.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["pending_decision"]["completed_stage"] == "module-2.1"
    assert state["pending_decision"]["next_stage"] == "module-2.2"
    assert state["pending_decision"]["status"] == "pending"
    assert budget.snapshot()["limits_enforced"] is False
    assert budget.snapshot()["hard_request_limits_enforced"] is False
    assert budget.snapshot()["boundary_policy_active"] is True


def test_same_run_resume_approves_one_new_window_without_repeating_work(tmp_path) -> None:
    run_id = "run-cost-resume"
    first = ReportingRunBudget(
        tmp_path,
        run_id,
        max_attempts=1,
        max_tokens=100,
        mode="pause_at_boundary",
    )
    _record_attempt(tmp_path, run_id)
    _commit_boundary(first, "module-2.1", "module-2.2")
    with pytest.raises(ReportingNeedsDecisionError):
        first.evaluate_boundary("module-2.1", "module-2.2")

    resumed = ReportingRunBudget(
        tmp_path,
        run_id,
        max_attempts=1,
        max_tokens=100,
        mode="pause_at_boundary",
    )
    assert resumed.resolve_resume() is True
    _commit_boundary(resumed, "module-2.1", "module-2.2")
    decision = resumed.evaluate_boundary("module-2.1", "module-2.2")

    assert decision["pause"] is False
    snapshot = resumed.snapshot()["cost_control"]
    assert snapshot["pending_decision"] is None
    assert snapshot["last_resolution"]["status"] == "continued_same_run"
    assert snapshot["next_provider_attempt_threshold"] == 2
    assert snapshot["next_total_token_threshold"] == 200


def test_warn_policy_records_and_advances_window_without_raising(tmp_path) -> None:
    run_id = "run-cost-warn"
    budget = ReportingRunBudget(
        tmp_path,
        run_id,
        max_attempts=1,
        max_tokens=100,
        mode="warn",
    )
    _record_attempt(tmp_path, run_id)
    _commit_boundary(budget, "module-2.1", "module-2.2")

    decision = budget.evaluate_boundary("module-2.1", "module-2.2")

    assert decision["action"] == "warn"
    assert decision["pause"] is False
    snapshot = budget.snapshot()
    assert snapshot["limits_enforced"] is False
    assert snapshot["uncached_input_tokens"] == 90
    assert snapshot["pricing_status"] == "unconfigured"
    assert snapshot["by_stage"]["module_authoring"]["provider_attempts"] == 1
    assert snapshot["cost_control"]["next_provider_attempt_threshold"] == 2


def test_all_workflow_operations_bind_the_requested_cost_control_mode() -> None:
    operations = (
        ReportWorkflowRunner.distill_template_skill,
        ReportWorkflowRunner.run,
        ReportWorkflowRunner.aggregate_existing,
        ReportWorkflowRunner.run_revision,
    )

    for operation in operations:
        source = inspect.getsource(operation)
        budget_construction = source[source.index("ReportingRunBudget(") :]
        assert "request.cost_control_mode" in budget_construction


@pytest.mark.asyncio
async def test_pause_mode_never_interrupts_an_active_typed_dispatch(tmp_path) -> None:
    run_id = "run-cost-active-turn"
    budget = ReportingRunBudget(
        tmp_path,
        run_id,
        max_attempts=1,
        max_tokens=100,
        mode="pause_at_boundary",
    )

    await budget.acquire("module-2.1-specialist")
    _record_attempt(tmp_path, run_id)
    # Provider admission remains observational while the typed turn is active.
    # The pause is evaluated only after its durable stage checkpoint commits.
    await budget.acquire_provider_attempt("module-2.1-specialist", "module-2.1")
    assert budget.snapshot()["active_dispatches"] == 1
    assert budget.snapshot()["cost_control"]["pending_decision"] is None
    await budget.release()

    _commit_boundary(budget, "module-authoring", "module-review")
    with pytest.raises(ReportingNeedsDecisionError):
        budget.evaluate_boundary("module-authoring", "module-review")

    assert budget.snapshot()["active_dispatches"] == 0


def test_boundary_evaluation_cannot_claim_an_unwritten_checkpoint(tmp_path) -> None:
    budget = ReportingRunBudget(
        tmp_path,
        "run-cost-no-checkpoint",
        max_attempts=1,
        max_tokens=100,
        mode="pause_at_boundary",
    )

    with pytest.raises(ValueError, match="checkpoint-committed"):
        budget.evaluate_boundary("module-2.1", "module-2.2")

    assert budget.snapshot()["cost_control"]["pending_decision"] is None


def test_prepared_boundary_survives_crash_and_is_evaluated_before_more_work(
    tmp_path,
) -> None:
    run_id = "run-cost-crash-window"
    first = ReportingRunBudget(
        tmp_path,
        run_id,
        max_attempts=1,
        max_tokens=100,
        mode="pause_at_boundary",
    )
    _record_attempt(tmp_path, run_id)
    prepared = first.prepare_boundary(
        "module-authoring",
        "module-review-2.1",
    )
    first.confirm_boundary_checkpoint(prepared["boundary_id"])

    resumed = ReportingRunBudget(
        tmp_path,
        run_id,
        max_attempts=1,
        max_tokens=100,
        mode="pause_at_boundary",
    )
    assert resumed.pending_boundary()["completed_stage"] == "module-authoring"
    with pytest.raises(ReportingNeedsDecisionError):
        resumed.evaluate_boundary(
            "module-authoring",
            "module-review-2.1",
        )

    snapshot = resumed.snapshot()["cost_control"]
    assert snapshot["pending_boundary_evaluation"] is None
    assert snapshot["pending_decision"]["next_stage"] == "module-review-2.1"


def test_uncommitted_boundary_intent_is_discarded_instead_of_false_pause(
    tmp_path,
) -> None:
    budget = ReportingRunBudget(
        tmp_path,
        "run-cost-uncommitted",
        max_attempts=1,
        max_tokens=100,
        mode="pause_at_boundary",
    )
    prepared = budget.prepare_boundary("module-authoring", "module-review")

    assert budget.discard_uncommitted_boundary(
        prepared["boundary_id"],
        reason="checkpoint_missing",
    )
    snapshot = budget.snapshot()["cost_control"]
    assert snapshot["pending_boundary_evaluation"] is None
    assert (
        snapshot["last_discarded_boundary"]["discard_reason"]
        == "checkpoint_missing"
    )


@pytest.mark.asyncio
async def test_recovered_pause_keeps_the_last_paid_work_checkpoint(
    tmp_path,
) -> None:
    run_id = "run-cost-preserve-checkpoint"
    budget = ReportingRunBudget(
        tmp_path,
        run_id,
        max_attempts=1,
        max_tokens=100,
        mode="pause_at_boundary",
    )
    _record_attempt(tmp_path, run_id)
    prepared = budget.prepare_boundary("module-authoring", "module-review")
    checkpoint = {
        "run_id": run_id,
        "activity": "module-work",
        "status": "in_progress",
        "completed_modules": ["2.1"],
        "preparation_refs": {"evidence": "Work/runs/x/evidence.jsonl"},
        "pending_cost_boundary_id": prepared["boundary_id"],
    }
    checkpoint_path = tmp_path / f"Work/runs/{run_id}/workflow-state.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(
        json.dumps(checkpoint, ensure_ascii=False),
        encoding="utf-8",
    )

    async def notice(_message: str) -> None:
        return None

    runner = object.__new__(ReportWorkflowRunner)
    runner.service = SimpleNamespace(workspace=tmp_path, _notice=notice)
    runner._budget = ReportingRunBudget(
        tmp_path,
        run_id,
        max_attempts=1,
        max_tokens=100,
        mode="pause_at_boundary",
    )

    with pytest.raises(ReportingNeedsDecisionError):
        await runner._recover_pending_cost_boundary(checkpoint)

    assert json.loads(checkpoint_path.read_text(encoding="utf-8")) == checkpoint


def test_resume_uses_business_recovery_store_not_workflow_state_checkpoint() -> None:
    source = inspect.getsource(ReportWorkflowRunner.run)
    recover = source.index("self._recover_pending_cost_boundary")
    prepare = source.index("await self._prepare")
    readiness = source.index("readiness = EvidenceReadinessPolicy.evaluate")
    first_checkpoint_after_readiness = source.index(
        "self._checkpoint",
        readiness,
    )
    first_boundary = source.index("self._checkpoint_then_cost_boundary")

    module_source = inspect.getsource(ReportWorkflowRunner._run_module_lanes)

    assert recover < prepare < readiness < first_checkpoint_after_readiness
    assert readiness < first_boundary
    assert "self._restore_resume_state" not in source
    assert "load_completed_lanes" in module_source
