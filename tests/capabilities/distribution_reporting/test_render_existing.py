"""Focused characterization for the file-defined render-existing entrypoint."""

import json
from pathlib import Path
from uuid import UUID

import pytest

from manyselves.application.runtime_services import RuntimeServicesView
from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.adapters.runtime import (
    DistributionReportingRuntimeBinding,
)
from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.kernel.definitions import DefinitionKind


def test_render_existing_is_declared_as_a_file_workflow_entrypoint() -> None:
    capability, registry = load_distribution_reporting_capability()

    assert "render-existing" in capability.entrypoints
    workflow = registry.require(DefinitionKind.WORKFLOW, "render-existing")
    assert workflow.id == "render-existing"


@pytest.mark.asyncio
async def test_render_existing_starts_through_the_generic_host(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Inputs" / "approved.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Approved\n\nExisting approved prose.\n", encoding="utf-8")

    binding = DistributionReportingRuntimeBinding(
        tmp_path,
        RuntimeServicesView(
            workspace=tmp_path,
            bus=MessageBus(),
            active_provider=None,
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
    )
    started = await binding.start(
        UUID("50000000-0000-4000-8000-000000000001"),
        "render-existing",
        {
            "operation": "render_existing",
            "instruction": "Render the approved Markdown as DOCX.",
            "source_markdown_ref": "Inputs/approved.md",
            "output_filename": "approved.docx",
        },
    )

    run_id = started["run_id"]
    run_root = tmp_path / "Work" / "runs" / run_id
    event_path = run_root / "workflow-events.jsonl"
    expected_files = (
        "input-snapshot.json",
        "resolved-plan.json",
        "runtime-state.json",
        "render-request.json",
        "template-provenance.json",
        "render-log.json",
        "render-result.json",
        "templates/report_template.docx",
    )
    assert all((run_root / relative).is_file() for relative in expected_files)
    assert binding.get_run(run_id)["run"]["status"] == "completed"
    outputs = binding.get_outputs(run_id)["outputs"]
    assert outputs[0]["id"] == "result"
    assert outputs[0]["value"]["output_ref"] == "Outputs/Reports/approved.docx"
    assert outputs[1] == {
        "id": "Outputs/Reports/approved.docx",
        "kind": "artifact",
        "path": "Outputs/Reports/approved.docx",
        "exists": True,
        "size": (tmp_path / "Outputs/Reports/approved.docx").stat().st_size,
    }
    render_request = json.loads(
        (run_root / "render-request.json").read_text(encoding="utf-8")
    )
    assert render_request["source_snapshot_ref"] == (
        f"Work/runs/{run_id}/frozen-project/Inputs/approved.md"
    )
    provenance = json.loads(
        (run_root / "template-provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["source"] == "packaged"
    assert provenance["selected_path"] == (
        "manyselves/templates/reporting/report_template.docx"
    )
    assert provenance["snapshot_path"] == "Work/runs/" + run_id + "/templates/report_template.docx"
    events = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["kind"] == "workflow.started"
    prepare_started = next(
        index
        for index, event in enumerate(events)
        if event["kind"] == "action.started"
        and event["action_id"] == "prepare-render"
    )
    completed = next(
        index for index, event in enumerate(events) if event["kind"] == "workflow.completed"
    )
    assert prepare_started < completed
