from pathlib import Path
from uuid import UUID

import pytest

from manyselves.application.workflow_projection import WorkflowProjectionFacade
from manyselves.core.usage_ledger import UsageLedger
from manyselves.webapi.main import create_app
from manyselves.webapi.settings import WebSettings


class _ReportingAdapter:
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

    def start(self, command_id: UUID, request: object) -> dict:
        self.calls.append(("start", request))
        return {"run_id": "report-new", "task_id": "task-new"}

    def start_declarative(self, command_id: UUID, request: object) -> dict:
        self.calls.append(("start_declarative", request))
        return {"run_id": "report-declarative-new", "task_id": "task-new"}

    def resume_run(self, command_id: UUID, run_id: str, **values: object) -> dict:
        self.calls.append(("resume", {"run_id": run_id, **values}))
        return {"run_id": run_id, "task_id": "task-resume"}

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


def test_capability_workflow_and_input_schema_are_generic_projections(
    tmp_path: Path,
) -> None:
    facade = WorkflowProjectionFacade(tmp_path, _ReportingAdapter())

    capabilities = facade.list_capabilities()
    workflows = facade.list_workflows()
    schema = facade.input_schema("distribution-reporting")

    assert capabilities == [
        {
            "id": "distribution-reporting",
            "version": "1.0.0",
            "description": "Declarative distribution reporting capability",
            "workflow_ids": [
                "distribution-module-cohort",
                "distribution-module-review-lane",
                "distribution-reporting",
                "distribution-reporting-tail",
            ],
        },
    ]
    assert next(item for item in workflows if item["id"] == "distribution-reporting") == {
        "id": "distribution-reporting",
        "capability_id": "distribution-reporting",
        "version": "1.0.0",
        "description": "Declarative top-level Reporting stage orchestration",
        "input_contract": "distribution_reporting_input",
        "output_contract": "distribution_reporting_output",
        "runnable": True,
    }
    assert schema["workflow_id"] == "distribution-reporting"
    assert schema["contract_id"] == "distribution_reporting_input"
    assert "instruction" in schema["schema"]["properties"]


def test_run_outputs_and_cost_reuse_current_reporting_state_without_new_hashes(
    tmp_path: Path,
) -> None:
    UsageLedger(tmp_path, "report-1").record_attempt(
        provider="openai",
        model="test-model",
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        stage="module",
    )
    facade = WorkflowProjectionFacade(tmp_path, _ReportingAdapter())

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


@pytest.mark.asyncio
async def test_run_start_and_input_delegate_to_the_current_reporting_adapter(
    tmp_path: Path,
) -> None:
    adapter = _ReportingAdapter()
    facade = WorkflowProjectionFacade(tmp_path, adapter)
    command_id = UUID("30000000-0000-4000-8000-000000000001")

    started = await facade.start(
        command_id,
        "distribution-reporting",
        {"instruction": "Generate the current report."},
    )
    resumed = facade.provide_input(
        command_id,
        "report-1",
        input_id=None,
        values={"supplements": []},
    )
    decided = facade.provide_input(
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
        "workflow_id": "distribution-reporting",
    }
    assert resumed["run_id"] == "report-1"
    assert decided["run_id"] == "report-1"
    assert [name for name, _ in adapter.calls] == [
        "start_declarative",
        "resume",
        "decision",
    ]


def test_openapi_exposes_the_generic_workflow_projection_paths(tmp_path: Path) -> None:
    paths = create_app(WebSettings(data_root=tmp_path)).openapi()["paths"]

    assert {
        "/api/v1/capabilities",
        "/api/v1/workflows",
        "/api/v1/workflows/{workflow_id}/input-schema",
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
        "/api/v1/runs/{run_id}/input",
        "/api/v1/runs/{run_id}/outputs",
        "/api/v1/runs/{run_id}/cost",
    } <= set(paths)
