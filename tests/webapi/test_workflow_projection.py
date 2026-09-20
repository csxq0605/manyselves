import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest

from manyselves.application.workflow_projection import WorkflowProjectionFacade
from manyselves.kernel.workflow import (
    ActionExecutionState,
    ResolvedPlan,
    WorkflowState,
    WorkflowStatus,
)
from manyselves.runtime.capability_binding import (
    CapabilityRunInputError,
    CapabilityRunNotFoundError,
    CapabilityRunStateError,
    RuntimeBindingCatalog,
)
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.usage_ledger import UsageLedger
from manyselves.runtime.workflow_host import FileWorkflowEventSink, WorkflowRuntimeEvent
from manyselves.webapi.main import create_app
from manyselves.webapi.routes.workflows import _error
from manyselves.webapi.schemas.workflows import (
    WorkflowRunInputRequest,
    WorkflowRunStartRequest,
)
from manyselves.webapi.settings import WebSettings


class _ReportingAdapter:
    capability_id = "distribution-reporting"

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def snapshot(self, run_id: str) -> dict:
        return {
            "run": {
                "run_id": run_id,
                "status": "completed",
                "active": False,
                "task_id": "task-1",
            },
            "state": {"status": "completed"},
            "waitingInput": [],
            "outputs": [
                {
                    "path": "Outputs/Reports/report.docx",
                    "exists": True,
                    "size": 4,
                    "sha256": "legacy-output-digest",
                }
            ],
        }

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: object,
    ) -> dict:
        del command_id, workflow_id
        self.calls.append(("start_declarative", values))
        return {"run_id": "report-declarative-new", "task_id": "task-new"}

    async def start_detached(
        self,
        command_id: UUID,
        workflow_id: str,
        values: object,
    ) -> dict:
        return await self.start(command_id, workflow_id, values)

    async def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: object,
    ) -> dict:
        del command_id
        runtime_state = None
        try:
            runtime_state = FileWorkflowStateStore(self.workspace).load(run_id)
        except (AttributeError, FileNotFoundError):
            pass
        if runtime_state is not None and runtime_state.status is WorkflowStatus.WAITING:
            self.calls.append(
                (
                    "workflow_input",
                    {"run_id": run_id, "input_id": input_id, "values": values},
                )
            )
            return {"run_id": run_id, "task_id": "task-workflow-input"}
        if input_id is not None:
            self.calls.append(("decision", {"decision_id": input_id, "values": values}))
            return {"run_id": run_id, "task_id": "task-decision"}
        self.calls.append(("resume", {"run_id": run_id, "values": values}))
        return {"run_id": run_id, "task_id": "task-resume"}

    def get_run(self, run_id: str) -> dict:
        snapshot = self.snapshot(run_id)
        try:
            runtime_state = FileWorkflowStateStore(self.workspace).load(run_id)
        except FileNotFoundError:
            runtime_state = None
        state = (
            runtime_state.model_dump(mode="json")
            if runtime_state is not None
            else snapshot["state"]
        )
        return {
            "run": {
                "run_id": run_id,
                "capability_id": self.capability_id,
                "workflow_id": state.get("workflow_id", "distribution-reporting"),
                "status": state.get("status", "completed"),
                "active": snapshot["run"]["active"],
                "task_id": snapshot["run"]["task_id"],
            },
            "state": state,
            "waiting_input": (
                [runtime_state.waiting_input]
                if runtime_state is not None
                and runtime_state.waiting_input is not None
                else snapshot["waitingInput"]
            ),
        }

    def get_outputs(self, run_id: str) -> dict:
        snapshot = self.snapshot(run_id)
        outputs: list[dict] = []
        try:
            runtime_state = FileWorkflowStateStore(self.workspace).load(run_id)
        except FileNotFoundError:
            runtime_state = None
        if runtime_state is not None and "result" in runtime_state.outputs:
            result = runtime_state.outputs["result"]
            public_result = result
            artifacts = []
            if isinstance(result, dict) and isinstance(result.get("output_artifacts"), list):
                artifacts = result["output_artifacts"]
                public_result = {
                    key: result[key]
                    for key in (
                        "run_id",
                        "delivery_completion_ref",
                        "delivery_status",
                        "output_artifacts",
                    )
                    if key in result
                }
            outputs.append({"id": "result", "kind": "value", "value": public_result})
            for artifact in artifacts:
                path = artifact["path"]
                target = self.workspace / path
                outputs.append(
                    {
                        "id": path,
                        "kind": "artifact",
                        "path": path,
                        "exists": target.is_file(),
                        "size": target.stat().st_size if target.is_file() else 0,
                    }
                )
        known_ids = {item["id"] for item in outputs}
        outputs.extend(
            {
                "id": item["path"],
                "kind": "artifact",
                "path": item["path"],
                "exists": item["exists"],
                "size": item["size"],
            }
            for item in snapshot["outputs"]
            if item["path"] not in known_ids
        )
        return {
            "run_id": run_id,
            "outputs": outputs,
        }

    def get_cost(self, run_id: str) -> dict:
        return {
            "run_id": run_id,
            "usage": UsageLedger(self.workspace, run_id).summarize(group_by="stage"),
        }

    def resume_run(self, command_id: UUID, run_id: str, **values: object) -> dict:
        self.calls.append(("resume", {"run_id": run_id, **values}))
        return {"run_id": run_id, "task_id": "task-resume"}

    async def resume(self, command_id: UUID, run_id: str) -> dict:
        self.calls.append(("resume_persisted", {"run_id": run_id}))
        return {"run_id": run_id, "task_id": None}

    def resume_workflow_input(
        self,
        command_id: UUID,
        run_id: str,
        input_id: str,
        values: object,
    ) -> dict:
        self.calls.append(
            (
                "workflow_input",
                {
                    "run_id": run_id,
                    "input_id": input_id,
                    "values": values,
                },
            )
        )
        return {"run_id": run_id, "task_id": "task-workflow-input"}

    def resume_decision(
        self,
        command_id: UUID,
        decision_id: str,
        action: str,
        supplements: list[object],
    ) -> dict:
        self.calls.append(
            (
                "decision",
                {
                    "decision_id": decision_id,
                    "action": action,
                    "supplements": supplements,
                },
            )
        )
        return {"run_id": "report-1", "task_id": "task-decision"}


def _facade(workspace: Path, binding: _ReportingAdapter) -> WorkflowProjectionFacade:
    binding.workspace = workspace
    bindings = RuntimeBindingCatalog()
    bindings.register(binding)
    return WorkflowProjectionFacade(
        workspace,
        runtime_services=None,
        runtime_bindings=bindings,
    )


def test_run_cost_projects_existing_mimo_rates_without_changing_ledger(tmp_path):
    path = tmp_path / ".manyselves/usage/report-priced.jsonl"
    path.parent.mkdir(parents=True)
    raw = json.dumps({
        "timestamp": "2026-09-09T12:00:00+08:00",
        "model": "mimo-v2.5", "input_tokens": 100,
        "cached_input_tokens": 20, "output_tokens": 10,
    }) + "\n"
    path.write_text(raw, encoding="utf-8")
    result = _facade(tmp_path, _ReportingAdapter()).get_cost("report-priced")
    usage = result["usage"]
    assert usage["totals"]["pricing_status"] == "estimated"
    assert "Token Plan Lite" in usage["pricing_summary"]
    assert "非实际账单" in usage["pricing_summary"]
    assert usage["pricing"]["priced_attempts"] == 1
    assert path.read_text(encoding="utf-8") == raw


def test_unknown_model_cost_remains_unknown(tmp_path):
    result = _facade(tmp_path, _ReportingAdapter()).get_cost("report-unpriced")
    assert result["usage"]["totals"]["pricing_status"] == "unconfigured"
    assert "pricing_summary" not in result["usage"]


class _DetachedReportingAdapter(_ReportingAdapter):
    """A binding exposing the persisted-state asynchronous start boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def start(self, command_id: UUID, workflow_id: str, values: object) -> dict:
        del command_id, workflow_id, values
        await self.release.wait()
        raise AssertionError("the blocking start path must not be awaited")

    async def start_detached(
        self,
        command_id: UUID,
        workflow_id: str,
        values: object,
    ) -> dict:
        del command_id, workflow_id
        self.calls.append(("start_detached", values))
        return {"run_id": "report-detached", "task_id": None}

    async def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: object,
    ) -> dict:
        del command_id
        self.calls.append(
            (
                "provide_input",
                {"run_id": run_id, "input_id": input_id, "values": values},
            )
        )
        return {"run_id": run_id, "task_id": None}


class _SelectiveReportingAdapter(_ReportingAdapter):
    def __init__(self, owned_run_ids: set[str]) -> None:
        super().__init__()
        self.owned_run_ids = owned_run_ids

    def get_run(self, run_id: str) -> dict:
        if run_id not in self.owned_run_ids:
            raise CapabilityRunNotFoundError(run_id)
        return super().get_run(run_id)


@pytest.mark.asyncio
async def test_start_uses_detached_binding_boundary_for_long_runs(
    tmp_path: Path,
) -> None:
    binding = _DetachedReportingAdapter()
    facade = _facade(tmp_path, binding)

    accepted = await asyncio.wait_for(
        facade.start(
            UUID("50000000-0000-4000-8000-000000000001"),
            "full-report",
            {"instruction": "long run"},
        ),
        timeout=0.2,
    )

    assert accepted == {
        "status": "accepted",
        "run_id": "report-detached",
        "task_id": None,
        "capability_id": "distribution-reporting",
        "workflow_id": "full-report",
    }
    assert binding.calls == [("start_detached", {"instruction": "long run"})]


@pytest.mark.asyncio
async def test_waiting_input_uses_binding_acceptance_boundary_for_long_runs(
    tmp_path: Path,
) -> None:
    binding = _DetachedReportingAdapter()
    facade = _facade(tmp_path, binding)
    values = {"answer": "continue the same Run"}

    accepted = await asyncio.wait_for(
        facade.provide_input(
            UUID("50000000-0000-4000-8000-000000000003"),
            "report-1",
            input_id="ask-evidence",
            values=values,
        ),
        timeout=0.2,
    )

    assert accepted["run_id"] == "report-1"
    assert binding.calls == [
        (
            "provide_input",
            {
                "run_id": "report-1",
                "input_id": "ask-evidence",
                "values": values,
            },
        )
    ]


def test_capability_workflow_and_input_schema_are_generic_projections(
    tmp_path: Path,
) -> None:
    facade = _facade(tmp_path, _ReportingAdapter())

    capabilities = facade.list_capabilities()
    workflows = facade.list_workflows()
    schema = facade.input_schema("full-report")

    assert [item["id"] for item in capabilities] == [
        "distribution-reporting",
        "parameter-adjustment",
    ]
    reporting = capabilities[0]
    assert reporting["version"] == "1.0.0"
    assert reporting["description"] == "Declarative distribution reporting capability"
    assert {
        "distribution-cross-owner-cohort",
        "distribution-module-cohort",
        "distribution-reporting",
        "distribution-reporting-tail",
        "distribution-report-delivery",
    } <= set(reporting["workflow_ids"])
    assert capabilities[1] == {
        "id": "parameter-adjustment",
        "version": "1.0.0",
        "description": "Neutral declarative parameter adjustment capability",
        "workflow_ids": ["parameter-adjustment"],
    }
    assert next(item for item in workflows if item["id"] == "full-report") == {
        "id": "full-report",
        "capability_id": "distribution-reporting",
        "version": "1.0.0",
        "description": "Public full Distribution Reporting workflow from preparation through delivery",
        "input_contract": "public_full_report_input",
        "output_contract": "distribution_reporting_output",
        "runnable": True,
    }
    assert schema["workflow_id"] == "full-report"
    assert schema["contract_id"] == "public_full_report_input"
    assert "instruction" in schema["schema"]["properties"]


def test_run_outputs_and_cost_reuse_current_reporting_state_without_new_hashes(
    tmp_path: Path,
) -> None:
    runtime_plan = ResolvedPlan(
        workflow_id="distribution-reporting",
        workflow_version="1.0.0",
        actions=[],
    )
    runtime_state = WorkflowState.for_plan("report-1", runtime_plan)
    runtime_state.status = WorkflowStatus.COMPLETED
    runtime_state.outputs = {
        "result": {"run_id": "report-1", "summary": "structured result"},
    }
    FileWorkflowStateStore(tmp_path).save(runtime_state)
    UsageLedger(tmp_path, "report-1").record_attempt(
        provider="openai",
        model="test-model",
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        stage="module",
    )
    facade = _facade(tmp_path, _ReportingAdapter())

    run = facade.get_run("report-1")
    outputs = facade.get_outputs("report-1")
    cost = facade.get_cost("report-1")

    assert run["run"] == {
        "run_id": "report-1",
        "capability_id": "distribution-reporting",
        "workflow_id": "distribution-reporting",
        "status": "completed",
        "active": False,
        "task_id": "task-1",
    }
    assert outputs == {
        "run_id": "report-1",
        "outputs": [
            {
                "id": "result",
                "kind": "value",
                "value": {"run_id": "report-1", "summary": "structured result"},
            },
            {
                "id": "Outputs/Reports/report.docx",
                "kind": "artifact",
                "path": "Outputs/Reports/report.docx",
                "exists": True,
                "size": 4,
            }
        ],
    }
    assert "sha256" not in outputs["outputs"][0]
    assert cost["run_id"] == "report-1"
    assert cost["usage"]["totals"]["total_tokens"] == 15


def test_reporting_outputs_project_declared_artifacts_without_internal_state(
    tmp_path: Path,
) -> None:
    class _CompletedReportingAdapter(_ReportingAdapter):
        def snapshot(self, run_id: str) -> dict:
            snapshot = super().snapshot(run_id)
            snapshot["outputs"] = []
            return snapshot

    artifact = tmp_path / "Outputs/Reports/report.docx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"docx")
    runtime_plan = ResolvedPlan(
        workflow_id="distribution-reporting",
        workflow_version="1.0.0",
        actions=[],
    )
    runtime_state = WorkflowState.for_plan("report-complete", runtime_plan)
    runtime_state.status = WorkflowStatus.COMPLETED
    runtime_state.outputs = {
        "result": {
            "run_id": "report-complete",
            "delivery_status": "delivered",
            "delivery_completion_ref": (
                "Work/runs/report-complete/delivery-completion.json"
            ),
            "output_artifacts": [
                {
                    "kind": "report",
                    "path": "Outputs/Reports/report.docx",
                    "module_id": None,
                }
            ],
            "module_submissions": {"2.1": {"internal": "not a public output"}},
        }
    }
    FileWorkflowStateStore(tmp_path).save(runtime_state)
    facade = _facade(tmp_path, _CompletedReportingAdapter())

    outputs = facade.get_outputs("report-complete")

    assert outputs == {
        "run_id": "report-complete",
        "outputs": [
            {
                "id": "result",
                "kind": "value",
                "value": {
                    "run_id": "report-complete",
                    "delivery_status": "delivered",
                    "delivery_completion_ref": (
                        "Work/runs/report-complete/delivery-completion.json"
                    ),
                    "output_artifacts": [
                        {
                            "kind": "report",
                            "path": "Outputs/Reports/report.docx",
                            "module_id": None,
                        }
                    ],
                },
            },
            {
                "id": "Outputs/Reports/report.docx",
                "kind": "artifact",
                "path": "Outputs/Reports/report.docx",
                "exists": True,
                "size": 4,
            },
        ],
    }


def _save_waiting_kernel_state(
    workspace: Path,
    run_id: str,
    waiting_input: dict[str, object],
) -> WorkflowState:
    plan = ResolvedPlan(
        workflow_id="distribution-reporting",
        workflow_version="1.0.0",
        actions=[],
    )
    state = WorkflowState.for_plan(run_id, plan)
    state.status = WorkflowStatus.WAITING
    state.waiting_input = waiting_input
    FileWorkflowStateStore(workspace).save(state)
    return state


def test_waiting_declarative_kernel_state_is_projected_as_run_input(
    tmp_path: Path,
) -> None:
    waiting_input = {
        "input_id": "ask-clarification",
        "interaction_id": "clarification",
        "contract_id": "clarification-input",
        "title": "Clarification",
        "description": "Collect one clarification",
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    }
    state = _save_waiting_kernel_state(
        tmp_path,
        "report-declarative-waiting",
        waiting_input,
    )
    facade = _facade(tmp_path, _ReportingAdapter())

    projection = facade.get_run("report-declarative-waiting")

    assert projection["run"]["status"] == "waiting"
    assert projection["state"] == {
        "run_id": state.run_id,
        "workflow_id": state.workflow_id,
        "status": state.status.value,
        "next_action_index": state.next_action_index,
        "control_steps": state.control_steps,
    }
    assert projection["waiting_input"] == [waiting_input]


def test_main_run_feed_lists_waiting_interactions_without_known_run_ids(
    tmp_path: Path,
) -> None:
    waiting_input = {
        "input_id": "request-main-exception-decision",
        "interaction_id": "module-main-exception-decision",
        "contract_id": "declarative_main_exception_user_input",
        "title": "Module exception decision",
        "description": "Continue, return, or stop this lane.",
        "schema": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": [
                        "accept_dispute",
                        "return_to_author",
                        "stop_incomplete",
                    ],
                }
            },
            "required": ["decision"],
        },
        "path": [
            {
                "kind": "parallel",
                "action_id": "run-module-lanes",
                "branch_id": "2.4",
            }
        ],
    }
    _save_waiting_kernel_state(tmp_path, "report-main-waiting", waiting_input)
    facade = _facade(tmp_path, _ReportingAdapter())

    listed = facade.list_runs()

    assert [item["run"]["run_id"] for item in listed] == ["report-main-waiting"]
    assert listed[0]["waiting_input"] == [waiting_input]


def test_run_list_keeps_owned_runs_when_a_historical_state_has_no_binding(
    tmp_path: Path,
) -> None:
    _save_waiting_kernel_state(tmp_path, "current-run", {"input_id": "current"})
    _save_waiting_kernel_state(tmp_path, "historical-unbound-run", {"input_id": "old"})
    facade = _facade(tmp_path, _SelectiveReportingAdapter({"current-run"}))

    listed = facade.list_runs()

    assert [item["run"]["run_id"] for item in listed] == ["current-run"]


def test_run_projection_bounds_large_kernel_state_and_preserves_runtime_fields(
    tmp_path: Path,
) -> None:
    waiting_input = {
        "input_id": "ask-clarification",
        "contract_id": "clarification-input",
        "title": "Clarification",
        "description": "Collect one clarification",
        "path": [{"action_id": "parent", "kind": "subworkflow"}],
        "schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
    }
    state = _save_waiting_kernel_state(tmp_path, "large-runtime-state", waiting_input)
    state.variables["full-report"] = {"content": "x" * 250_000}
    state.actions["run-module-cohort"] = ActionExecutionState(
        error="module cohort failed"
    )
    FileWorkflowStateStore(tmp_path).save(state)

    facade = _facade(tmp_path, _ReportingAdapter())
    projection = facade.get_run("large-runtime-state")

    assert projection["run"]["status"] == "waiting"
    assert projection["state"]["status"] == "waiting"
    assert projection["state"]["error"] == "module cohort failed"
    assert projection["waiting_input"] == [waiting_input]
    assert "variables" not in projection["state"]
    assert len(json.dumps(projection)) < 100_000


@pytest.mark.asyncio
async def test_waiting_declarative_input_uses_workflow_resume_contract_for_generic_values(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-input"
    waiting_input = {
        "input_id": "ask-clarification",
        "interaction_id": "clarification",
        "contract_id": "clarification-input",
        "title": "Clarification",
        "description": "Collect one clarification",
        "schema": {"type": "object"},
    }
    _save_waiting_kernel_state(tmp_path, run_id, waiting_input)
    adapter = _ReportingAdapter()
    facade = _facade(tmp_path, adapter)
    values: dict[str, object] = {
        "answer": "Ada",
        "arbitrary": [1, True, {"nested": "value"}],
    }

    accepted = await facade.provide_input(
        UUID("30000000-0000-4000-8000-000000000002"),
        run_id,
        input_id="ask-clarification",
        values=values,
    )

    assert accepted["run_id"] == run_id
    assert [name for name, _ in adapter.calls] == ["workflow_input"]
    assert adapter.calls[0] == (
        "workflow_input",
        {
            "run_id": run_id,
            "input_id": "ask-clarification",
            "values": values,
        },
    )


def test_generic_waiting_input_request_accepts_any_json_contract_value() -> None:
    request = WorkflowRunInputRequest.model_validate(
        {"inputId": "ask-number", "values": 7}
    )

    assert request.input_id == "ask-number"
    assert request.values == 7


def test_generic_start_request_accepts_any_json_contract_value() -> None:
    request = WorkflowRunStartRequest.model_validate(
        {"workflowId": "primitive-workflow", "input": [1, 2, 3]}
    )

    assert request.input == [1, 2, 3]


@pytest.mark.asyncio
async def test_existing_decision_input_without_kernel_waiting_state_uses_resume_decision(
    tmp_path: Path,
) -> None:
    adapter = _ReportingAdapter()
    facade = _facade(tmp_path, adapter)

    accepted = await facade.provide_input(
        UUID("30000000-0000-4000-8000-000000000003"),
        "report-1",
        input_id="decision-1",
        values={"action": "draft", "supplements": []},
    )

    assert accepted["run_id"] == "report-1"
    assert [name for name, _ in adapter.calls] == ["decision"]


def test_run_events_project_file_sink_for_current_run_only(tmp_path: Path) -> None:
    sink = FileWorkflowEventSink(tmp_path)
    sink.append(
        WorkflowRuntimeEvent(
            kind="workflow.started",
            run_id="report-1",
            workflow_id="distribution-reporting",
            data={"source": "runtime-host"},
        )
    )
    sink.append(
        WorkflowRuntimeEvent(
            kind="workflow.completed",
            run_id="other-run",
            workflow_id="distribution-reporting",
        )
    )

    facade = _facade(tmp_path, _ReportingAdapter())

    assert facade.get_events("report-1") == {
        "run_id": "report-1",
        "events": [
            {
                "kind": "workflow.started",
                "run_id": "report-1",
                "workflow_id": "distribution-reporting",
                "action_id": None,
                "error": None,
                "data": {"source": "runtime-host"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_run_start_and_input_delegate_to_the_capability_binding(
    tmp_path: Path,
) -> None:
    adapter = _ReportingAdapter()
    facade = _facade(tmp_path, adapter)
    command_id = UUID("30000000-0000-4000-8000-000000000001")

    started = await facade.start(
        command_id,
        "full-report",
        {"instruction": "Generate the current report."},
    )
    resumed = await facade.provide_input(
        command_id,
        "report-1",
        input_id=None,
        values={"supplements": []},
    )
    decided = await facade.provide_input(
        command_id,
        "report-1",
        input_id="decision-1",
        values={"action": "draft", "supplements": []},
    )

    assert started == {
        "status": "accepted",
        "run_id": "report-declarative-new",
        "task_id": "task-new",
        "capability_id": "distribution-reporting",
        "workflow_id": "full-report",
    }
    assert resumed["run_id"] == "report-1"
    assert decided["run_id"] == "report-1"
    assert [name for name, _ in adapter.calls] == [
        "start_declarative",
        "resume",
        "decision",
    ]


@pytest.mark.asyncio
async def test_resume_run_delegates_to_generic_binding_boundary(
    tmp_path: Path,
) -> None:
    adapter = _ReportingAdapter()
    facade = _facade(tmp_path, adapter)

    resumed = await facade.resume(
        UUID("30000000-0000-4000-8000-000000000005"),
        "report-1",
    )

    assert resumed == {
        "status": "accepted",
        "run_id": "report-1",
        "task_id": None,
        "capability_id": "distribution-reporting",
        "workflow_id": "distribution-reporting",
    }
    assert adapter.calls == [("resume_persisted", {"run_id": "report-1"})]


def test_openapi_exposes_the_generic_workflow_projection_paths(tmp_path: Path) -> None:
    paths = create_app(WebSettings(data_root=tmp_path)).openapi()["paths"]

    assert {
        "/api/v1/capabilities",
        "/api/v1/workflows",
        "/api/v1/workflows/{workflow_id}/input-schema",
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
        "/api/v1/runs/{run_id}/input",
        "/api/v1/runs/{run_id}/resume",
        "/api/v1/runs/{run_id}/outputs",
        "/api/v1/runs/{run_id}/cost",
        "/api/v1/runs/{run_id}/events",
    } <= set(paths)
    assert "get" in paths["/api/v1/runs"]


def test_generic_runtime_input_error_maps_without_reporting_exception_types() -> None:
    error = _error(CapabilityRunInputError("input rejected"))

    assert error.status_code == 422
    assert error.code == "WORKFLOW_INPUT_INVALID"


def test_generic_runtime_state_error_maps_without_reporting_exception_types() -> None:
    error = _error(CapabilityRunStateError("state unreadable"))

    assert error.status_code == 500
    assert error.code == "WORKFLOW_STATE_INVALID"
