"""Run the full report on the generic Runtime.

The report Capability supplies only its public input projection and the Python
implementations of the Tools its file-defined workflow reaches; compiling,
executing, recovering and projecting the Run are the generic binding's job.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding

from .. import CAPABILITY_FILE
from ..domain.photo_bindings import runtime_photo_ids
from .entrypoint_tools import build_public_entrypoint_tool_implementations
from .evidence_readiness import build_evidence_readiness_tool_implementations
from .input_snapshot import RunInputSnapshotStore
from .ledger_tools import LedgerTools
from .models.reporting import ReportRequest, ReportRevision
from .photo_reading import build_photo_reading_tool_implementations
from .preparation_tools import build_preparation_tool_implementations
from .public_entrypoints import project_public_entrypoint_input
from .run_taxonomy import bind_run_taxonomy, run_taxonomy_scope
from .storage import ReportingStore


def ledger_report_tool_implementations(
    workspace: Path,
    *,
    input_snapshot: Any,
    snapshot_content: Any,
    global_knowledge_root: Path | None = None,
) -> dict[str, Any]:
    store = ReportingStore(workspace)
    # Every call reads chapter 2's tree from the run it serves.
    return bind_run_taxonomy({
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
    }, workspace)


def ledger_report_start_input(workspace: Path):
    def start_input(run_id: str, workflow_id: str, values: Any) -> dict[str, Any]:
        request = project_public_entrypoint_input(workflow_id, run_id, values)
        if not isinstance(request, ReportRequest):
            raise ValueError(f"{workflow_id} does not take a report request")
        RunInputSnapshotStore(workspace).freeze(run_id)
        return {"request": request, "run-id": run_id}

    return start_input


# The frozen inputs stay with the run; a revision with new files re-freezes them.
KEPT_ON_RERUN = ("input-snapshot.json", "frozen-project")


def ledger_report_rerun_input(workspace: Path):
    """Initial variables for running a report again under the same run id.

    Without revision values the report runs again from its saved request.
    With them it becomes a revision of the version just archived.  The
    request stays exactly as it was and the revision goes in its own
    variable, read only by the ledger, so every step before the ledger sees
    the same input and is skipped; with ``new_inputs`` the project's current
    files are frozen again for this version, and the steps that read them
    run again.
    """

    def rerun_input(
        run_id: str,
        workflow_id: str,
        values: Any,
        previous: dict[str, Any],
        archive: Path,
    ) -> dict[str, Any]:
        request = previous.get("request")
        if not isinstance(request, dict):
            raise ValueError(f"run {run_id} has no saved report request")
        request = ReportRequest.model_validate(request).model_dump(mode="json")
        if values is None:
            return {"request": request, "run-id": run_id}
        if not (archive / "ledger" / "judgments.json").is_file():
            raise ValueError(
                f"run {run_id} has no finished ledger to revise; resume or run it again first"
            )
        revision = ReportRevision.model_validate(
            {**dict(values), "base_version": _version_number(archive)}
        )
        if revision.new_inputs:
            # The base version keeps the inputs it was made from.
            for name in KEPT_ON_RERUN:
                source = Path(workspace) / "Work/runs" / run_id / name
                if source.exists():
                    shutil.move(str(source), str(archive / name))
            RunInputSnapshotStore(workspace).freeze(run_id)
        del workflow_id
        return {"request": request, "run-id": run_id, "revision": revision.model_dump(mode="json")}

    return rerun_input


def _version_number(archive: Path) -> int:
    digits = archive.name.removeprefix("v")
    if not digits.isdigit():
        raise ValueError(f"not a version directory: {archive}")
    return int(digits)


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
        rerun_input=ledger_report_rerun_input(Path(workspace)),
        frozen_inputs=KEPT_ON_RERUN,
        run_context=lambda run_id: run_taxonomy_scope(Path(workspace), run_id),
    )


__all__ = [
    "KEPT_ON_RERUN",
    "build_ledger_report_binding",
    "ledger_report_rerun_input",
    "ledger_report_start_input",
    "ledger_report_tool_implementations",
]
