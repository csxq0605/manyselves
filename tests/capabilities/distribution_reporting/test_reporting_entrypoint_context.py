"""Characterize the typed boundary between top-level file workflows."""

from __future__ import annotations

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.domain.coverage import (
    evaluate_coverage,
)
from manyselves.capabilities.distribution_reporting.runtime.entrypoint_tools import (
    attach_preparation,
    build_entrypoint_tool_implementations,
    build_reporting_state,
    initialize_reporting_run,
)
from manyselves.capabilities.distribution_reporting.runtime.models.entrypoint import (
    ReportingRunContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    PreparationContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    ReportRequest,
    SourceLocation,
)
from manyselves.kernel.definitions import DefinitionKind, ToolDefinition


def _request() -> ReportRequest:
    return ReportRequest(
        operation="module_report",
        instruction="Generate only module 2.4 from current project evidence.",
        target_modules=["2.4"],
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )


def test_entrypoint_projection_tools_are_file_declared() -> None:
    _capability, registry = load_distribution_reporting_capability()
    implementations = build_entrypoint_tool_implementations()

    assert set(implementations) == {
        "initialize-reporting-run",
        "attach-reporting-preparation",
        "build-reporting-state",
    }
    for tool_id in implementations:
        definition = registry.require(DefinitionKind.TOOL, tool_id)
        assert isinstance(definition, ToolDefinition)
        assert definition.model_visible is False


def test_entrypoint_context_projects_preparation_without_legacy_runner_state() -> None:
    request = _request()
    context = initialize_reporting_run(
        {"run_id": "entrypoint-context", "request": request}
    )

    assert isinstance(context, ReportingRunContext)
    assert context.run_id == "entrypoint-context"
    assert context.request is request
    assert context.preparation_context == PreparationContext(
        run_id="entrypoint-context",
        request=request,
    )

    evidence = EvidenceItem(
        id="E-0001",
        subject="breaker inspection",
        fact="The breaker inspection completed.",
        source=SourceLocation(file_id="inspection", path="Inputs/inspection.md"),
    )
    prepared = context.preparation_context.model_copy(
        update={
            "evidence_items": [evidence],
            "coverage_matrix": evaluate_coverage(request, [evidence]),
            "input_snapshot_ref": (
                "Work/runs/entrypoint-context/input-snapshot.json"
            ),
            "preparation_completion_ref": (
                "Work/runs/entrypoint-context/preparation/completion.json"
            ),
        }
    )
    attached = attach_preparation({"context": context, "preparation": prepared})
    reporting_state = build_reporting_state(attached)

    assert attached.preparation_context is prepared
    assert reporting_state["run_id"] == "entrypoint-context"
    assert reporting_state["request"] is request
    assert reporting_state["resume"] is False
    assert reporting_state["evidence_items"] == [evidence]
    assert reporting_state["coverage_matrix"] == prepared.coverage_matrix
    assert reporting_state["input_snapshot_ref"] == prepared.input_snapshot_ref
    assert (
        reporting_state["preparation_completion_ref"]
        == prepared.preparation_completion_ref
    )
    assert reporting_state["requested_modules"] == ("2.4",)
    assert reporting_state["full_report"] is False


def test_full_report_context_uses_the_fixed_module_set() -> None:
    request = ReportRequest(
        operation="full_report",
        instruction="Generate the full report.",
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )
    context = initialize_reporting_run(
        {"run_id": "full-entrypoint-context", "request": request}
    )
    prepared = context.preparation_context.model_copy(
        update={"coverage_matrix": evaluate_coverage(request, [])}
    )

    reporting_state = build_reporting_state(
        attach_preparation({"context": context, "preparation": prepared})
    )

    assert reporting_state["requested_modules"] == (
        "2.1",
        "2.2",
        "2.3",
        "2.4",
        "2.5",
    )
    assert reporting_state["full_report"] is True
