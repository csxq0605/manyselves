"""Cross owner inputs above the request budget are reviewed in dossier batches."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_composition import (
    build_cross_owner_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_definitions import (
    register_cross_owner_pipeline_specializations,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerRuntime,
    _input_tokens,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossOwnerFindingSubmission,
    CrossReviewCoverageEntry,
)
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerInitialAgentResult,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import CrossOwnerInput
from manyselves.config.schema import AgentDefaults
from manyselves.kernel.conversations import ConversationRegistry
from manyselves.kernel.definitions import DefinitionKind
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.context_budget import retained_input_budget, retained_input_budget_for
from manyselves.runtime.state_store import InMemoryWorkflowStateStore
from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink, WorkflowRuntimeHost
from tests.capabilities.distribution_reporting.test_cross_owner_workflow_host import (
    _OWNER,
    _reporting_state,
    _TypedCrossScript,
)


def _dossier(number: int) -> dict[str, Any]:
    return {
        "dossier_id": f"D-{number:03d}",
        "subject": f"{number}#配电房/G{number}",
        "reasons": ["modules_disagree"],
        "module_ids": ["2.1", "2.2"],
        "submodules": [
            {"module_id": "2.1", "submodule_id": "2.1.1"},
            {"module_id": "2.2", "submodule_id": "2.2.1"},
        ],
        "facts": [{
            "evidence_id": f"E-{number:04d}",
            "module_id": "2.1",
            "submodule_id": "2.1.1",
            "fact": f"{number}号柜运行记录。" + "现场记录内容" * 400,
        }],
        "judgments": [],
    }


def _with_dossiers(workspace: Path, state: dict[str, Any], count: int) -> dict[str, Any]:
    ref = f"Work/runs/{state['run_id']}/reviews/joint-dossiers.json"
    path = workspace / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"dossiers": [_dossier(n) for n in range(1, count + 1)]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {**state, "joint_dossiers_ref": ref}


def _budget_below_full_input(workspace: Path, state: dict[str, Any]) -> int:
    full = CrossOwnerRuntime(workspace, input_budget=10**9).prepare(dict(state))
    return _input_tokens(full["cross_owner_inputs"][_OWNER]) - 1


def _submission(finding_ids: tuple[str, ...] = ()) -> DeclarativeCrossOwnerInitialAgentResult:
    template = _TypedCrossScript("unused")._result("cross-owner-runtime-initial-review").submission
    findings = [template.findings[0].model_copy(update={"id": item}) for item in finding_ids]
    return DeclarativeCrossOwnerInitialAgentResult(
        status="completed",
        submission=CrossOwnerFindingSubmission(
            owner_module_id=_OWNER,
            coverage=CrossReviewCoverageEntry(module_id=_OWNER, checked_dimensions=["facts"]),
            findings=findings,
        ),
    )


def test_budget_follows_compaction_target_and_window() -> None:
    defaults = AgentDefaults()
    assert retained_input_budget_for(defaults) == int(
        defaults.working_memory_tokens * defaults.working_memory_compaction_target_ratio
    )
    # A small window is the tighter bound.
    assert retained_input_budget(
        context_window=32_000, max_output=8_192, working_memory_tokens=72_000,
        compaction_target_ratio=0.6,
    ) < 32_000 - 8_192


def test_inputs_within_budget_stay_one_turn(tmp_path: Path) -> None:
    state = _with_dossiers(tmp_path, _reporting_state(tmp_path, "one-batch"), 2)
    prepared = CrossOwnerRuntime(tmp_path).prepare(state)
    assert prepared["cross_owner_input_batches"] == {}
    context = CrossOwnerRuntime(tmp_path).prepare_initial(
        {"state": prepared, "owner_module_id": _OWNER}
    )
    assert context.initial_batches == []
    assert context.preparation.envelope.task_id == "cross-owner-2.1-r0-initial"


def test_oversized_owner_input_splits_by_dossier_and_merges(tmp_path: Path) -> None:
    state = _with_dossiers(tmp_path, _reporting_state(tmp_path, "split"), 3)
    runtime = CrossOwnerRuntime(tmp_path, input_budget=_budget_below_full_input(tmp_path, state))
    prepared = runtime.prepare(state)
    batches = prepared["cross_owner_input_batches"][_OWNER]
    assert len(batches) >= 2
    assert sorted(d for batch in batches for d in batch["dossier_ids"]) == ["D-001", "D-002", "D-003"]
    for batch in batches:
        contract = json.loads((tmp_path / batch["ref"]).read_text(encoding="utf-8"))
        assert [d["dossier_id"] for d in contract["joint_dossiers"]] == batch["dossier_ids"]
        assert contract["owner_scope_submodule_ids"] == ["2.1.1"]
        assert (
            _input_tokens(CrossOwnerInput.model_validate(contract)) <= runtime._input_budget()
            or len(batch["dossier_ids"]) == 1
        )

    context = runtime.prepare_initial({"state": prepared, "owner_module_id": _OWNER})
    assert runtime.initial_requires_agent(context)
    items = runtime.initial_batch_items(context)
    assert items == [{"batch_index": i} for i in range(len(batches))]
    task_ids = set()
    for item in items:
        context = runtime.select_initial_batch({"context": context, "batch": item})
        task_ids.add(context.preparation.envelope.task_id)
        # Every batch reports the same id; the merged review keeps both findings.
        context = runtime.accept_initial({"context": context, "result": _submission(("X-2.1-r0-1",))})
    assert len(task_ids) == len(batches)
    merged = runtime.merge_initial_batches(context)
    assert merged.status == "initial_accepted"
    ids = [finding.id for finding in merged.acceptance.result.findings]
    assert ids[0] == "X-2.1-r0-1" and len(set(ids)) == len(batches)
    assert merged.initial_finding_batches == {fid: i for i, fid in enumerate(ids)}
    assert merged.preparation.owner_input.joint_dossiers == prepared[
        "cross_owner_inputs"][_OWNER].joint_dossiers
    assert (tmp_path / merged.acceptance.result_ref).is_file()


def test_same_run_resume_skips_reviewed_batches(tmp_path: Path) -> None:
    state = _with_dossiers(tmp_path, _reporting_state(tmp_path, "resume"), 3)
    runtime = CrossOwnerRuntime(tmp_path, input_budget=_budget_below_full_input(tmp_path, state))
    prepared = runtime.prepare(state)
    context = runtime.prepare_initial({"state": prepared, "owner_module_id": _OWNER})
    first = runtime.select_initial_batch({"context": context, "batch": {"batch_index": 0}})
    runtime.accept_initial({"context": first, "result": _submission()})

    resumed = runtime.prepare_initial({"state": prepared, "owner_module_id": _OWNER})
    again = runtime.select_initial_batch({"context": resumed, "batch": {"batch_index": 0}})
    assert not runtime.initial_requires_agent(again)
    second = runtime.select_initial_batch({"context": resumed, "batch": {"batch_index": 1}})
    assert runtime.initial_requires_agent(second)
    assert resumed.preparation.batch_index == 1


class _BatchScript(_TypedCrossScript):
    """Only the first batch has a finding; later batches are clean."""

    def _result(self, task_id: str, *, occurrence: int = 1) -> Any:
        result = super()._result(task_id, occurrence=occurrence)
        if task_id == "cross-owner-runtime-initial-review" and occurrence > 1:
            result.submission.findings = []
        return result


@pytest.mark.asyncio
async def test_generic_host_runs_one_turn_per_batch_and_rechecks_its_batch(tmp_path: Path) -> None:
    run_id = "cross-batches-host"
    _capability, registry = load_distribution_reporting_capability()
    register_cross_owner_pipeline_specializations(registry)
    workflow = registry.require(DefinitionKind.WORKFLOW, "distribution-cross-owner-2.1-pipeline")
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)
    state = _with_dossiers(tmp_path, _reporting_state(tmp_path, run_id), 3)
    runtime = CrossOwnerRuntime(tmp_path, input_budget=_budget_below_full_input(tmp_path, state))
    script = _BatchScript(run_id)
    context = RuntimeContext(
        tools=build_cross_owner_tool_implementations(runtime),
        agents={
            "cross-module-reviewer": script,
            "module-2.1-specialist": script,
            "evidence-auditor": script,
        },
        conversations=ConversationRegistry(),
    )
    final = await asyncio.wait_for(WorkflowRuntimeHost(
        build_builtin_executor_registry(),
        InMemoryWorkflowStateStore(),
        InMemoryWorkflowEventSink(),
    ).execute(plan, WorkflowState.for_plan(
        run_id, plan, initial_variables={"reporting-state": state},
    ), context), timeout=20)

    assert final.status == "completed"
    assert final.outputs["result"].status == "completed"
    initial_inputs = [
        value.preparation for task_id, value in script.inputs
        if task_id == "cross-owner-runtime-initial-review"
    ]
    assert len(initial_inputs) >= 2
    assert len({item.envelope.task_id for item in initial_inputs}) == len(initial_inputs)
    first_batch = initial_inputs[0].batch_dossier_ids
    recheck_input = json.loads((
        tmp_path / f"Work/runs/{run_id}/reviews/cross-owner-input-r1-2.1.json"
    ).read_text(encoding="utf-8"))
    assert [d["dossier_id"] for d in recheck_input["joint_dossiers"]] == first_batch
