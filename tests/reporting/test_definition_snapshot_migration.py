"""Characterization for the private Reporting definition-snapshot migration.

The real Run was compiled with a v1.0.0 snapshot of
``final-chief-chapter-revision``.  The packaged definition is now v1.0.1, but
ordinary resume must continue to use the frozen plan unless this narrowly
targeted migration is explicitly applied.  These tests describe that one
compatibility operation; they do not exercise the Provider or the reporting
workflow itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from manyselves.core.reporting.declarative_plan_migration import (
    migrate_final_chief_revision_task_snapshot,
)
from manyselves.kernel.definitions import DefinitionKind, WorkflowDefinition, load_definition
from manyselves.kernel.workflow import (
    ActionExecutionStatus,
    EndWorkflowAction,
    ResolvedPlan,
    SetVariableAction,
    SubworkflowAction,
    WorkflowState,
    WorkflowStatus,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGED_TASK = (
    REPOSITORY_ROOT
    / "manyselves"
    / "capabilities"
    / "distribution_reporting"
    / "tasks"
    / "final-chief-chapter-revision.yaml"
)
RUN_ID = "report-declarative-33ed40175b"


def _definition_snapshot(definition: WorkflowDefinition) -> dict[str, Any]:
    return definition.model_dump(mode="json", by_alias=True)


def _old_task_snapshot() -> dict[str, Any]:
    packaged = load_definition(PACKAGED_TASK, expected_kind=DefinitionKind.TASK)
    return packaged.model_copy(
        update={"version": "1.0.0", "tools": ["submit_result"]}
    ).model_dump(mode="json", by_alias=True)


def _plan() -> ResolvedPlan:
    child = ResolvedPlan(
        workflow_id="final-review-child",
        workflow_version="1.0.0",
        actions=[
            SetVariableAction(id="child.prepare", variable="child_ready", value=True),
            EndWorkflowAction(
                id="child.finish",
                output_variable="child_ready",
                output_name="result",
            ),
        ],
        initial_state={"child_ready": False},
        entry_action_id="child.prepare",
        workflow_ids=["final-review-child"],
        definition_snapshots={
            "workflow:final-review-child": _definition_snapshot(
                WorkflowDefinition(
                    id="final-review-child",
                    version="1.0.0",
                    description="Nested characterization workflow",
                )
            )
        },
    )
    return ResolvedPlan(
        workflow_id="distribution-reporting",
        workflow_version="1.0.0",
        actions=[
            SetVariableAction(id="root.prepare", variable="request", value={"chapter": 1}),
            SubworkflowAction(
                id="root.review-child",
                workflow="final-review-child",
                input_variable="request",
                output_variable="child_output",
            ),
            EndWorkflowAction(
                id="root.finish",
                output_variable="child_output",
                output_name="result",
            ),
        ],
        initial_state={"request": None, "child_output": None},
        entry_action_id="root.prepare",
        workflow_ids=["distribution-reporting", "final-review-child"],
        task_ids=["final-chief-chapter-revision"],
        subworkflow_plans={"final-review-child": child},
        definition_snapshots={
            "workflow:distribution-reporting": _definition_snapshot(
                WorkflowDefinition(
                    id="distribution-reporting",
                    version="1.0.0",
                    description="Distribution Reporting characterization workflow",
                    actions=[
                        {"id": action.id, "kind": action.kind.value}
                        for action in (
                            SetVariableAction(
                                id="root.prepare", variable="request", value={"chapter": 1}
                            ),
                            SubworkflowAction(
                                id="root.review-child",
                                workflow="final-review-child",
                                input_variable="request",
                                output_variable="child_output",
                            ),
                            EndWorkflowAction(
                                id="root.finish",
                                output_variable="child_output",
                            ),
                        )
                    ],
                )
            ),
            "workflow:final-review-child": _definition_snapshot(
                WorkflowDefinition(
                    id="final-review-child",
                    version="1.0.0",
                    description="Nested characterization workflow",
                )
            ),
            "task:final-chief-chapter-revision": _old_task_snapshot(),
            "task:unchanged": {
                "kind": "task",
                "id": "unchanged",
                "version": "1.0.0",
                "description": "Unchanged task snapshot",
                "agent": "chief-editor",
                "objective": "Remain byte-for-byte unchanged",
                "input_contract": "input",
                "output_contract": "output",
                "tools": ["submit_result"],
            },
        },
    )


def _state(plan: ResolvedPlan) -> WorkflowState:
    state = WorkflowState.for_plan(
        RUN_ID,
        plan,
        initial_variables={"request": {"chapter": 1}},
    )
    state.status = WorkflowStatus.RUNNING
    state.actions["root.prepare"].status = ActionExecutionStatus.COMPLETED
    state.next_action_index = 1
    state.next_action_id = "root.review-child"
    state.subworkflow_states["root.review-child"] = {
        "workflow_id": "final-review-child",
        "status": "waiting",
        "next_action_id": "child.finish",
    }
    state.waiting_input = {
        "input_id": "chapter-confirmation",
        "path": ["root.review-child", "child.finish"],
    }
    return state


def _write_run_snapshot(workspace: Path, plan: ResolvedPlan, state: WorkflowState) -> Path:
    run_dir = workspace / "Work" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "resolved-plan.json").write_text(
        plan.model_dump_json(), encoding="utf-8"
    )
    (run_dir / "runtime-state.json").write_text(
        state.model_dump_json(), encoding="utf-8"
    )
    (run_dir / "workflow-events.jsonl").write_text("", encoding="utf-8")
    return run_dir


def test_migrates_only_target_task_snapshot_and_records_event(tmp_path: Path) -> None:
    plan = _plan()
    state = _state(plan)
    original_plan = plan.model_dump(mode="json")
    original_state = state.model_dump(mode="json")
    run_dir = _write_run_snapshot(tmp_path, plan, state)

    assert migrate_final_chief_revision_task_snapshot(tmp_path, RUN_ID) is True

    packaged = load_definition(PACKAGED_TASK, expected_kind=DefinitionKind.TASK)
    migrated = ResolvedPlan.model_validate_json((run_dir / "resolved-plan.json").read_text())
    migrated_state = WorkflowState.model_validate_json(
        (run_dir / "runtime-state.json").read_text()
    )
    target = migrated.definition_snapshots["task:final-chief-chapter-revision"]
    assert target == packaged.model_dump(mode="json", by_alias=True)
    assert target["version"] == "1.0.1"
    assert target["tools"] == ["write_result_part", "list_result_parts", "submit_result"]

    before_snapshots = plan.definition_snapshots
    after_snapshots = migrated.definition_snapshots
    assert set(after_snapshots) == set(before_snapshots)
    for key, snapshot in before_snapshots.items():
        if key != "task:final-chief-chapter-revision":
            assert after_snapshots[key] == snapshot

    # A definition repair must not rewrite the executable graph or persisted state.
    migrated_graph = migrated.model_copy(update={"definition_snapshots": {}})
    original_graph = plan.model_copy(update={"definition_snapshots": {}})
    assert migrated_graph == original_graph
    assert migrated_state.model_dump(mode="json") == original_state
    assert plan.model_dump(mode="json") == original_plan

    migration_events = [
        json.loads(line)
        for line in (run_dir / "workflow-events.jsonl").read_text().splitlines()
        if line
    ]
    assert len(migration_events) == 1
    event = migration_events[0]
    assert event["kind"] == "definition.migrated"
    assert event["run_id"] == RUN_ID
    assert event["workflow_id"] == "distribution-reporting"
    assert event["data"]["definition_kind"] == "task"
    assert event["data"]["definition_id"] == "final-chief-chapter-revision"
    assert event["data"]["from_version"] == "1.0.0"
    assert event["data"]["to_version"] == "1.0.1"


def test_reapplying_migration_is_a_no_op_without_another_event(tmp_path: Path) -> None:
    plan = _plan()
    state = _state(plan)
    run_dir = _write_run_snapshot(tmp_path, plan, state)

    assert migrate_final_chief_revision_task_snapshot(tmp_path, RUN_ID) is True
    migrated_plan = (run_dir / "resolved-plan.json").read_text(encoding="utf-8")
    first_events = (run_dir / "workflow-events.jsonl").read_text(encoding="utf-8")

    assert migrate_final_chief_revision_task_snapshot(tmp_path, RUN_ID) is False
    assert (run_dir / "resolved-plan.json").read_text(encoding="utf-8") == migrated_plan
    assert (run_dir / "workflow-events.jsonl").read_text(encoding="utf-8") == first_events


def test_migration_rejects_an_unrelated_source_version_without_writes(
    tmp_path: Path,
) -> None:
    plan = _plan()
    snapshots = dict(plan.definition_snapshots)
    snapshots["task:final-chief-chapter-revision"] = {
        **snapshots["task:final-chief-chapter-revision"],
        "version": "0.9.0",
    }
    plan = plan.model_copy(update={"definition_snapshots": snapshots})
    run_dir = _write_run_snapshot(tmp_path, plan, _state(plan))
    original_plan = (run_dir / "resolved-plan.json").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="must be v1.0.0"):
        migrate_final_chief_revision_task_snapshot(tmp_path, RUN_ID)

    assert (run_dir / "resolved-plan.json").read_text(encoding="utf-8") == original_plan
    assert (run_dir / "workflow-events.jsonl").read_text(encoding="utf-8") == ""
