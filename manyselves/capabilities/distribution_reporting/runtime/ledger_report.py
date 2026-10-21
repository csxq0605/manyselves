"""Run the full report on the generic Runtime.

The report Capability supplies only its public input projection and the Python
implementations of the Tools its file-defined workflow reaches; compiling,
executing, recovering and projecting the Run are the generic binding's job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding

from .. import CAPABILITY_FILE
from ..domain.photo_bindings import runtime_photo_ids
from .entrypoint_tools import build_public_entrypoint_tool_implementations
from .evidence_readiness import build_evidence_readiness_tool_implementations
from .input_snapshot import RunInputSnapshotStore
from .ledger_tools import LedgerTools
from .models.reporting import ReportRequest
from .photo_reading import build_photo_reading_tool_implementations
from .preparation_tools import build_preparation_tool_implementations
from .public_entrypoints import project_public_entrypoint_input
from .storage import ReportingStore


def ledger_report_tool_implementations(
    workspace: Path,
    *,
    input_snapshot: Any,
    snapshot_content: Any,
    global_knowledge_root: Path | None = None,
) -> dict[str, Any]:
    store = ReportingStore(workspace)
    return {
        **build_public_entrypoint_tool_implementations(),
        **build_preparation_tool_implementations(
            workspace=workspace,
            input_snapshot=input_snapshot,
            snapshot_content=snapshot_content,
            runtime_photo_ids=runtime_photo_ids,
            store=store,
            global_knowledge_root=global_knowledge_root,
        ),
        **build_evidence_readiness_tool_implementations(),
        **build_photo_reading_tool_implementations(workspace, store),
        **LedgerTools(workspace, global_knowledge_root=global_knowledge_root).implementations(),
    }


def ledger_report_start_input(workspace: Path):
    def start_input(run_id: str, workflow_id: str, values: Any) -> dict[str, Any]:
        request = project_public_entrypoint_input(workflow_id, run_id, values)
        if not isinstance(request, ReportRequest):
            raise ValueError(f"{workflow_id} does not take a report request")
        RunInputSnapshotStore(workspace).freeze(run_id)
        return {"request": request, "run-id": run_id}

    return start_input


def build_ledger_report_binding(
    workspace: Path,
    services: Any,
    *,
    input_snapshot: Any,
    snapshot_content: Any,
    loop_builder: Any | None = None,
    execution: Any | None = None,
) -> GenericCapabilityRuntimeBinding:
    return GenericCapabilityRuntimeBinding(
        capability_file=CAPABILITY_FILE,
        workspace=workspace,
        services=services,
        tool_implementations=ledger_report_tool_implementations(
            Path(workspace),
            input_snapshot=input_snapshot,
            snapshot_content=snapshot_content,
            global_knowledge_root=getattr(services, "global_knowledge_root", None),
        ),
        loop_builder=loop_builder,
        start_input=ledger_report_start_input(Path(workspace)),
        execution=execution,
    )


__all__ = [
    "build_ledger_report_binding",
    "ledger_report_start_input",
    "ledger_report_tool_implementations",
]
