"""Explicit one-time migrations for frozen declarative Reporting plans."""

from __future__ import annotations

from pathlib import Path

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.kernel.definitions import DefinitionKind, TaskDefinition
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import FileWorkflowEventSink, WorkflowRuntimeEvent

_FINAL_CHIEF_REVISION_TASK_ID = "final-chief-chapter-revision"
_FINAL_CHIEF_REVISION_SNAPSHOT_KEY = f"task:{_FINAL_CHIEF_REVISION_TASK_ID}"
_FINAL_CHIEF_REVISION_SOURCE_VERSION = "1.0.0"
_FINAL_CHIEF_REVISION_TARGET_VERSION = "1.0.1"


def migrate_final_chief_revision_task_snapshot(
    workspace: Path,
    run_id: str,
) -> bool:
    """Replace only the defective v1.0.0 Final Chief Task snapshot.

    Ordinary resume keeps a Run's resolved plan immutable.  This private,
    explicitly invoked compatibility operation updates the one packaged Task
    definition that lacked its result-part Tools, while leaving the executable
    graph and WorkflowState untouched.  ``True`` means the migration was
    applied; ``False`` means the target definition was already present.
    """

    state_store = FileWorkflowStateStore(workspace)
    plan = state_store.load_plan(run_id)
    try:
        source = plan.definition_snapshots[_FINAL_CHIEF_REVISION_SNAPSHOT_KEY]
    except KeyError as exc:
        raise ValueError(
            "resolved plan does not contain final-chief-chapter-revision"
        ) from exc

    source_version = str(source.get("version", ""))
    if source_version == _FINAL_CHIEF_REVISION_TARGET_VERSION:
        return False
    if source_version != _FINAL_CHIEF_REVISION_SOURCE_VERSION:
        raise ValueError(
            "final-chief-chapter-revision snapshot must be v1.0.0 before migration"
        )

    _capability, definitions = load_distribution_reporting_capability()
    target = definitions.require(
        DefinitionKind.TASK,
        _FINAL_CHIEF_REVISION_TASK_ID,
    )
    if not isinstance(target, TaskDefinition):
        raise TypeError("final-chief-chapter-revision is not a Task definition")
    if target.version != _FINAL_CHIEF_REVISION_TARGET_VERSION:
        raise ValueError(
            "packaged final-chief-chapter-revision definition must be v1.0.1"
        )

    snapshots = dict(plan.definition_snapshots)
    snapshots[_FINAL_CHIEF_REVISION_SNAPSHOT_KEY] = target.model_dump(
        mode="json",
        by_alias=True,
    )
    migrated = plan.model_copy(update={"definition_snapshots": snapshots})
    state_store.save_plan(run_id, migrated)
    FileWorkflowEventSink(workspace).append(
        WorkflowRuntimeEvent(
            kind="definition.migrated",
            run_id=run_id,
            workflow_id=plan.workflow_id,
            data={
                "definition_kind": DefinitionKind.TASK.value,
                "definition_id": _FINAL_CHIEF_REVISION_TASK_ID,
                "from_version": _FINAL_CHIEF_REVISION_SOURCE_VERSION,
                "to_version": _FINAL_CHIEF_REVISION_TARGET_VERSION,
            },
        )
    )
    return True


__all__ = ["migrate_final_chief_revision_task_snapshot"]
