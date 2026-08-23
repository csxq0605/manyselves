"""Characterization for the production public Reporting runtime binding."""

from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting.runtime import preparation_tools
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
    PublicReportingWorkflowRuntime,
)
from manyselves.kernel.workflow import WorkflowStatus
from manyselves.runtime.state_store import InMemoryWorkflowStateStore
from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink


def test_public_reporting_runtime_binding_module_exists() -> None:
    """Characterize the missing Capability-owned root runtime before implementation."""

    assert find_spec(
        "manyselves.capabilities.distribution_reporting.runtime.public_reporting"
    ) is not None


class _BoundaryAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invoke(self, _agent, task, _value, _conversation, *, task_id: str):
        self.calls.append(task.id)
        raise RuntimeError(
            "stopped at the existing module Agent adapter boundary"
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
    ):
        return await self.invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
        )


class _BoundaryModuleRuntime:
    def __init__(self, agent: _BoundaryAgent) -> None:
        self.agent_invokers = {"module-2.4-specialist": agent}

    async def prepare_lanes(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    async def start_lane(
        self,
        module_id: str,
        state: dict[str, Any],
        workflow_id: str,
        _lane_outcome=None,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return DeclarativeModuleRuntimeLaneContext(
            module_id=module_id,
            workflow_id=workflow_id,
            reporting_state=dict(state),
            status="ready",
        )

    async def prepare_author_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context.model_copy(update={"status": "author_ready"})

    async def author_requires_agent(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return True

    async def lane_retries_preflight_revision(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def lane_has_deferred_main_exception(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False


def _patch_minimal_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot_digest = "a" * 64

    monkeypatch.setattr(
        preparation_tools,
        "parse_report_taxonomy_workbook",
        lambda *args, **kwargs: {"schema_version": 1, "modules": []},
    )
    monkeypatch.setattr(preparation_tools, "activate_report_taxonomy", lambda _value: None)
    monkeypatch.setattr(
        preparation_tools,
        "prepare_manifest_file",
        lambda workspace, run_id, manifest_file, order: preparation_tools.FilePreparationResult(
            manifest_order=order,
            file_id=manifest_file.id,
            source_path=manifest_file.path,
            source_sha256=snapshot_digest,
            purpose=manifest_file.purpose,
            status="parsed",
        ),
    )


@pytest.mark.asyncio
async def test_module_report_host_reaches_only_selected_module_agent_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preparation/readiness complete before the selected module Agent boundary."""

    _patch_minimal_preparation(monkeypatch)
    snapshot = {
        "inventory_digest": "inventory",
        "files": [
            {
                "logical_ref": "Inputs/S4-6.xlsx",
                "snapshot_ref": "Inputs/S4-6.xlsx",
                "sha256": "a" * 64,
            }
        ],
    }
    agent = _BoundaryAgent()
    module_runtime = _BoundaryModuleRuntime(agent)
    store = InMemoryWorkflowStateStore()
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: snapshot,
        snapshot_content=lambda source, target: (target, "a" * 64, "blob"),
        runtime_photo_ids=lambda _evidence, _photos: None,
        module_runtime=module_runtime,
        state_store=store,
        events=InMemoryWorkflowEventSink(),
    )
    request = ReportRequest(
        operation="module_report",
        instruction="run only module 2.4",
        target_modules=["2.4"],
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )
    _definitions, _contracts, plan = runtime.compile_plan(request)
    assert set(plan.subworkflow_plans) >= {
        "distribution-reporting-preparation",
        "distribution-evidence-readiness",
        "distribution-module-cohort-selected-2-4",
        "distribution-module-2.4-runtime-lane",
    }
    assert not any(
        module_id in subworkflow_id
        for subworkflow_id in plan.subworkflow_plans
        for module_id in ("2.1", "2.2", "2.3", "2.5")
    )

    run_id = "public-module-boundary"
    with pytest.raises(RuntimeError, match="existing module Agent adapter boundary"):
        await runtime.execute(request, run_id)

    state = store.load(run_id)
    assert state.status is WorkflowStatus.FAILED
    assert state.actions["run-reporting-preparation"].status.value == "completed"
    assert state.actions["run-evidence-readiness"].status.value == "completed"
    assert state.actions["build-reporting-state"].status.value == "completed"
    assert agent.calls == ["module-2.4-authoring"]


def test_full_report_stops_at_existing_tail_specialization_boundary(
    tmp_path: Path,
) -> None:
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot={},
        snapshot_content=lambda *args: None,
        runtime_photo_ids=lambda *args: None,
    )
    request = ReportRequest(
        operation="full_report",
        instruction="compile the full report root",
    )

    with pytest.raises(
        ValueError,
        match="distribution-cross-owner-2.1-pipeline",
    ):
        runtime.compile_plan(request)
