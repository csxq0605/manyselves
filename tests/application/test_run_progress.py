"""The progress view reads where a Run is from its persisted state tree."""

from __future__ import annotations

import json
from pathlib import Path

from manyselves.application.run_progress import RunProgressReader

CHILD = {
    "workflow_id": "child",
    "actions": [
        {"id": "open", "kind": "create_conversation"},
        {"id": "each", "kind": "for_each", "items_variable": "sections",
         "item_variable": "section", "body": "write", "after": "done"},
        {"id": "write", "kind": "invoke_agent"},
        {"id": "done", "kind": "end_workflow"},
    ],
}
PLAN = {
    "workflow_id": "root",
    "actions": [
        {"id": "prep", "kind": "invoke_tool"},
        {"id": "items", "kind": "set_variable"},
        {"id": "fan", "kind": "map", "items_variable": "groups", "key_field": "id",
         "output_variable": "written",
         "workflow": "child"},
        {"id": "par", "kind": "parallel", "branches": {"a": "step-a", "b": "step-b"},
         "join": "join"},
        {"id": "step-a", "kind": "invoke_tool"},
        {"id": "a-done", "kind": "goto", "target": "join"},
        {"id": "step-b", "kind": "subworkflow", "workflow": "child"},
        {"id": "b-done", "kind": "goto", "target": "join"},
        {"id": "join", "kind": "join"},
        {"id": "compose", "kind": "invoke_tool"},
        {"id": "finish", "kind": "end_workflow"},
    ],
    "subworkflow_plans": {"child": CHILD},
}


def _child(status: str, *, index: int | None = None) -> dict:
    state = {"workflow_id": "child", "status": status, "actions": {}}
    if index is not None:
        state["next_action_id"] = "write"
        state["actions"] = {"open": {"status": "completed"}}
        state["control_frames"] = {"each": {"items": ["2.1", "2.2", "2.3"], "index": index}}
    return state


def _write(tmp_path: Path, state: dict, events: list[dict] | None = None) -> RunProgressReader:
    run_dir = tmp_path / "Work" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "resolved-plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
    (run_dir / "runtime-state.json").write_text(json.dumps(state), encoding="utf-8")
    if events is not None:
        (run_dir / "workflow-events.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )
    return RunProgressReader(tmp_path)


def test_map_shows_each_running_item_and_the_queue(tmp_path: Path) -> None:
    reader = _write(
        tmp_path,
        {
            "run_id": "run-1",
            "workflow_id": "root",
            "status": "running",
            "next_action_id": "fan",
            "variables": {"groups": [{"id": "G1"}, {"id": "G2"}, {"id": "G3"}, {"id": "G4"}]},
            "actions": {"prep": {"status": "completed"}, "items": {"status": "completed"}},
            "map_states": {"fan": {"G1": _child("completed"), "G2": _child("running", index=1),
                                   "G3": _child("running", index=0)}},
        },
    )

    view = reader.read("run-1")
    root = view["root"]
    assert [(s["id"], s["status"]) for s in root["steps"]] == [
        ("prep", "completed"),
        ("fan", "running"),
        ("par", "pending"),
        ("step-a", "pending"),
        ("step-b", "pending"),
        ("compose", "pending"),
    ]
    fan = root["detail"]
    assert fan["kind"] == "map"
    assert (fan["total"], fan["completed"], fan["running"], fan["queued"]) == (4, 1, 2, 1)
    assert fan["queued_keys"] == ["G4"]
    g2 = fan["items"][0]
    assert g2["key"] == "G2"
    assert g2["node"]["detail"] == {"kind": "for_each", "action_id": "each", "index": 1, "total": 3}
    assert [s["id"] for s in g2["node"]["steps"]] == ["each", "write"]


def test_parallel_branches_report_their_own_steps(tmp_path: Path) -> None:
    reader = _write(
        tmp_path,
        {
            "run_id": "run-1",
            "workflow_id": "root",
            "status": "running",
            "next_action_id": "par",
            "actions": {
                "prep": {"status": "completed"},
                "fan": {"status": "completed"},
                "par": {"status": "running"},
            },
            "parallel_states": {
                "par": {
                    "a": {"workflow_id": "root", "status": "completed"},
                    "b": {
                        "workflow_id": "root",
                        "status": "running",
                        "next_action_id": "step-b",
                        "actions": {},
                        "subworkflow_states": {"step-b": _child("running", index=2)},
                    },
                }
            },
        },
    )

    root = reader.read("run-1")["root"]
    statuses = {s["id"]: s["status"] for s in root["steps"]}
    assert statuses["step-a"] == "completed"
    assert statuses["step-b"] == "running"
    branches = {b["branch_id"]: b for b in root["detail"]["branches"]}
    assert branches["a"]["status"] == "completed"
    b_node = branches["b"]["node"]
    assert [s["id"] for s in b_node["steps"]] == ["step-b"]
    assert b_node["detail"]["child"]["detail"]["index"] == 2


def test_a_failed_run_shows_where_it_stopped_and_the_newest_events_first(
    tmp_path: Path,
) -> None:
    reader = _write(
        tmp_path,
        {
            "run_id": "run-1",
            "workflow_id": "root",
            "status": "failed",
            "next_action_id": "compose",
            "actions": {
                "prep": {"status": "completed"},
                "fan": {"status": "completed"},
                "par": {"status": "completed"},
                "compose": {"status": "failed", "error": "boom"},
            },
        },
        events=[
            {"kind": "action.started", "run_id": "run-1", "workflow_id": "root",
             "action_id": "compose", "data": {}},
            {"kind": "map.item.completed", "run_id": "run-1", "workflow_id": "root",
             "action_id": "fan", "data": {"key": "G1"}, "ts": "2026-10-23T14:00:00+00:00"},
            {"kind": "action.failed", "run_id": "run-1", "workflow_id": "root",
             "action_id": "compose", "error": "boom", "data": {}},
            {"kind": "workflow.failed", "run_id": "other", "workflow_id": "root", "data": {}},
        ],
    )

    view = reader.read("run-1")
    statuses = {s["id"]: s["status"] for s in view["root"]["steps"]}
    assert statuses["compose"] == "failed"
    assert statuses["step-a"] == "completed"
    assert [(e["kind"], e["key"]) for e in view["events"]] == [
        ("action.failed", None),
        ("map.item.completed", "G1"),
    ]
    assert view["events"][0]["error"] == "boom"


def test_reading_is_cached_until_the_state_file_changes(tmp_path: Path) -> None:
    state = {"run_id": "run-1", "workflow_id": "root", "status": "running",
             "next_action_id": "prep", "actions": {}}
    reader = _write(tmp_path, state)
    first = reader.read("run-1")
    assert reader.read("run-1") is first
    path = tmp_path / "Work" / "runs" / "run-1" / "runtime-state.json"
    path.write_text(json.dumps({**state, "status": "completed"}) + " ", encoding="utf-8")
    assert reader.read("run-1")["status"] == "completed"


def test_workflow_agents_count_as_busy_until_they_go_idle() -> None:
    from manyselves.application.runtime_state import RuntimeStateProjection
    from manyselves.interfaces.types import AgentStatus, StatusChange

    projection = RuntimeStateProjection()
    writer = "distribution-reporting:full-report-1:report-consultant:writer:2.4#1"
    projection.observe(StatusChange(agent_type=writer, status=AgentStatus.THINKING))
    projection.observe(StatusChange(agent_type="main", status=AgentStatus.THINKING))

    assert projection.busy_agents(known={"main": "idle"}) == {writer: "thinking"}

    projection.observe(StatusChange(agent_type=writer, status=AgentStatus.IDLE))
    assert projection.busy_agents(known={"main": "idle"}) == {}
    assert projection.busy_agents(known=(), stale_after_seconds=-1) == {}


def test_spans_time_each_step_from_its_first_start_to_its_last_finish(tmp_path: Path) -> None:
    def event(kind: str, action: str, ts: str | None) -> dict:
        row = {"kind": kind, "run_id": "run-1", "workflow_id": "root", "action_id": action, "data": {}}
        if ts:
            row["ts"] = ts
        return row

    reader = _write(
        tmp_path,
        {"run_id": "run-1", "workflow_id": "root", "status": "running",
         "next_action_id": "fan", "actions": {}},
        events=[
            event("action.started", "prep", None),
            event("action.started", "fan", "2026-10-23T14:00:00+00:00"),
            event("action.started", "fan", "2026-10-23T14:00:05+00:00"),
            event("action.completed", "fan", "2026-10-23T14:01:00+00:00"),
            event("action.completed", "fan", "2026-10-23T14:00:30+00:00"),
        ],
    )

    spans = reader.read("run-1")["spans"]
    assert spans == {
        "root/fan": {"start": "2026-10-23T14:00:00+00:00", "end": "2026-10-23T14:01:00+00:00"}
    }
