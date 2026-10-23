"""Read where a Run is now, from its persisted state tree.

The Runtime Host checkpoints the whole nested state of a Run into
``runtime-state.json`` after every transition: sub-workflows under
``subworkflow_states``, map items under ``map_states``, parallel branches under
``parallel_states`` and loop positions under ``control_frames``.  This module
walks that tree next to the frozen plan (``resolved-plan.json``) and returns a
small, bounded view for a progress panel:

- the visible steps of each workflow, in plan order, with their status;
- for the step that is running, what it is doing now: the child workflow, the
  map's items (done / running / failed / queued, and each running item's own
  step), the parallel branches, or the loop position.

It also returns the newest lines of the Run's event trace.  It knows no
Capability: labels and explanations belong to whoever shows the view.
Reading is cached by file size and modification time.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from manyselves.kernel.workflow.models import MapAction
from manyselves.kernel.workflow.records import RecordError, map_items

# Steps a person can follow; bookkeeping steps are left out of the view.
_VISIBLE_KINDS = frozenset(
    {
        "invoke_tool",
        "invoke_agent",
        "map",
        "parallel",
        "subworkflow",
        "for_each",
        "request_input",
        "wait_input",
        "publish_result",
    }
)
_DONE = "completed"
_MAX_DEPTH = 6
_MAX_ITEMS = 40
_RECENT_EVENTS = 40
_TAIL_BYTES = 96_000
_EVENT_KINDS = frozenset(
    {
        "workflow.started",
        "workflow.completed",
        "workflow.failed",
        "workflow.waiting",
        "action.failed",
        "action.waiting",
        "agent.invoked",
        "tool.invoked",
        "map.item.completed",
        "map.item.failed",
        "output.published",
    }
)


class RunProgressReader:
    """Project a Run's state tree into a progress view, cached per file."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self._plans: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}

    def read(self, run_id: str) -> dict[str, Any]:
        run_dir = self._workspace / "Work" / "runs" / run_id
        state_path = run_dir / "runtime-state.json"
        plan_path = run_dir / "resolved-plan.json"
        events_path = run_dir / "workflow-events.jsonl"
        key = (_stamp(state_path), _stamp(plan_path), _stamp(events_path))
        with self._lock:
            cached = self._cache.get(run_id)
            if cached is not None and cached[0] == key:
                return cached[1]
        if key[0] is None:
            raise FileNotFoundError(run_id)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        plan = self._plan(run_id, plan_path, key[1])
        view: dict[str, Any] = {
            "run_id": run_id,
            "workflow_id": str(state.get("workflow_id") or ""),
            "status": str(state.get("status") or ""),
            "root": _describe(state, plan, depth=0) if plan else None,
            "events": _recent_events(events_path, run_id),
        }
        with self._lock:
            self._cache[run_id] = (key, view)
        return view

    def _plan(self, run_id: str, path: Path, stamp: Any) -> dict[str, Any]:
        if stamp is None:
            return {}
        with self._lock:
            cached = self._plans.get(run_id)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        plan = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._plans[run_id] = (stamp, plan)
        return plan


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _describe(
    state: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    depth: int,
    segment: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """One workflow (or one parallel branch of it): steps and the running detail."""

    actions = [a for a in plan.get("actions") or () if isinstance(a, Mapping)]
    order = [str(a.get("id")) for a in actions]
    by_id = {str(a.get("id")): a for a in actions}
    if segment is not None:
        start, join = segment
        actions = _branch_segment(actions, start, join)
    status = str(state.get("status") or "pending")
    current = state.get("next_action_id")
    current = str(current) if isinstance(current, str) else None
    branch_owner = _branch_owners(actions if segment is None else (), by_id)
    steps = []
    for action in actions:
        action_id = str(action.get("id"))
        kind = str(action.get("kind"))
        if kind not in _VISIBLE_KINDS:
            continue
        step_status = _step_status(state, action_id, current, status, order, branch_owner)
        step: dict[str, Any] = {"id": action_id, "kind": kind, "status": step_status}
        if kind in {"subworkflow", "map"}:
            step["workflow"] = str(action.get("workflow") or "")
        steps.append(step)
    node: dict[str, Any] = {
        "workflow_id": str(state.get("workflow_id") or plan.get("workflow_id") or ""),
        "status": status,
        "current": current,
        "steps": steps,
        "detail": None,
    }
    if status == _DONE and depth > 0:
        return node
    if current is not None and current in by_id and depth < _MAX_DEPTH:
        node["detail"] = _detail(state, plan, by_id[current], depth=depth)
    return node


def _detail(
    state: Mapping[str, Any],
    plan: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    depth: int,
) -> dict[str, Any] | None:
    action_id = str(action.get("id"))
    kind = str(action.get("kind"))
    child_plans = plan.get("subworkflow_plans") or {}
    if kind == "subworkflow":
        child = (state.get("subworkflow_states") or {}).get(action_id)
        child_plan = child_plans.get(str(action.get("workflow")))
        if not isinstance(child, Mapping) or not isinstance(child_plan, Mapping):
            return {"kind": kind, "action_id": action_id, "child": None}
        return {
            "kind": kind,
            "action_id": action_id,
            "child": _describe(child, _with_children(child_plan, child_plans), depth=depth + 1),
        }
    if kind == "map":
        return _map_detail(state, plan, action, depth=depth)
    if kind == "parallel":
        saved = (state.get("parallel_states") or {}).get(action_id) or {}
        branches = []
        for branch_id, start in (action.get("branches") or {}).items():
            branch_state = saved.get(branch_id)
            if isinstance(branch_state, Mapping):
                branch = _describe(
                    branch_state,
                    plan,
                    depth=depth + 1,
                    segment=(str(start), str(action.get("join"))),
                )
            else:
                branch = None
            branches.append(
                {
                    "branch_id": str(branch_id),
                    "start": str(start),
                    "status": str(branch_state.get("status"))
                    if isinstance(branch_state, Mapping)
                    else "pending",
                    "node": branch,
                }
            )
        return {"kind": kind, "action_id": action_id, "branches": branches}
    return _loop_detail(state)


def _loop_detail(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """The innermost open loop of this workflow: which item it is on."""

    for loop_id, frame in reversed(list((state.get("control_frames") or {}).items())):
        if isinstance(frame, Mapping) and isinstance(frame.get("items"), list):
            return {
                "kind": "for_each",
                "action_id": str(loop_id),
                "index": int(frame.get("index") or 0),
                "total": len(frame["items"]),
            }
    return None


def _map_detail(
    state: Mapping[str, Any],
    plan: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    depth: int,
) -> dict[str, Any]:
    action_id = str(action.get("id"))
    child_plans = plan.get("subworkflow_plans") or {}
    child_plan = child_plans.get(str(action.get("workflow")))
    saved = (state.get("map_states") or {}).get(action_id) or {}
    keys = _map_keys(state, action)
    total = len(keys) if keys is not None else len(saved)
    counts = {"completed": 0, "running": 0, "failed": 0, "waiting": 0}
    items = []
    for key, item_state in saved.items():
        if not isinstance(item_state, Mapping):
            continue
        item_status = str(item_state.get("status") or "running")
        if item_status == "pending":
            item_status = "running"
        counts[item_status] = counts.get(item_status, 0) + 1
        if item_status == _DONE:
            continue
        node = None
        if isinstance(child_plan, Mapping):
            node = _describe(
                item_state, _with_children(child_plan, child_plans), depth=depth + 1
            )
        items.append({"key": str(key), "status": item_status, "node": node})
    items.sort(key=lambda row: (row["status"] != "failed", row["key"]))
    started = sum(counts.values())
    order = list(keys) if keys is not None else []
    queued = [key for key in order if key not in saved]
    return {
        "kind": "map",
        "action_id": action_id,
        "workflow": str(action.get("workflow") or ""),
        "total": max(total, started),
        "completed": counts["completed"],
        "running": counts["running"] + counts.get("waiting", 0),
        "failed": counts["failed"],
        "queued": max(0, max(total, started) - started),
        "items": items[:_MAX_ITEMS],
        "completed_keys": [
            str(key)
            for key, item_state in saved.items()
            if isinstance(item_state, Mapping) and item_state.get("status") == _DONE
        ][:_MAX_ITEMS * 4],
        "queued_keys": queued[:_MAX_ITEMS],
    }


def _map_keys(state: Mapping[str, Any], action: Mapping[str, Any]) -> list[str] | None:
    variables = state.get("variables") or {}
    collection = variables.get(str(action.get("items_variable")))
    if collection is None:
        return None
    try:
        parsed = MapAction.model_validate(dict(action))
        return [
            key
            for key, _item in map_items(
                collection,
                key_field=parsed.key_field,
                where=parsed.where,
                owner=parsed.id,
            )
        ]
    except (RecordError, ValueError, TypeError):
        pass
    # A plan this reader cannot parse: count the collection as it stands.
    if isinstance(collection, Mapping):
        return [str(key) for key in collection]
    if isinstance(collection, list):
        field = action.get("key_field")
        return [
            str(item.get(field)) if field and isinstance(item, Mapping) else str(index)
            for index, item in enumerate(collection)
        ]
    return None


def _with_children(child_plan: Mapping[str, Any], plans: Mapping[str, Any]) -> dict[str, Any]:
    """Child plans resolve their own children from the root's plan bundle."""

    if child_plan.get("subworkflow_plans"):
        return dict(child_plan)
    return {**child_plan, "subworkflow_plans": plans}


def _branch_segment(
    actions: list[Mapping[str, Any]], start: str, join: str
) -> list[Mapping[str, Any]]:
    ids = [str(a.get("id")) for a in actions]
    if start not in ids:
        return []
    segment = []
    for action in actions[ids.index(start):]:
        if str(action.get("id")) == join:
            break
        if action.get("kind") == "goto" and str(action.get("target")) == join:
            break
        segment.append(action)
    return segment


def _branch_owners(
    actions: Any, by_id: Mapping[str, Mapping[str, Any]]
) -> dict[str, tuple[str, str]]:
    """Map each action inside a parallel branch to (parallel id, branch id)."""

    owners: dict[str, tuple[str, str]] = {}
    actions = list(actions)
    for action in actions:
        if action.get("kind") != "parallel":
            continue
        join = str(action.get("join"))
        for branch_id, start in (action.get("branches") or {}).items():
            for inner in _branch_segment(actions, str(start), join):
                owners[str(inner.get("id"))] = (str(action.get("id")), str(branch_id))
    return owners


def _step_status(
    state: Mapping[str, Any],
    action_id: str,
    current: str | None,
    workflow_status: str,
    order: list[str],
    branch_owner: Mapping[str, tuple[str, str]],
) -> str:
    owner = branch_owner.get(action_id)
    if owner is not None:
        parallel_id, branch_id = owner
        parallel = (state.get("actions") or {}).get(parallel_id) or {}
        if parallel.get("status") == _DONE or workflow_status == _DONE:
            return _DONE
        branch = ((state.get("parallel_states") or {}).get(parallel_id) or {}).get(branch_id)
        if not isinstance(branch, Mapping):
            return "pending"
        if branch.get("status") == _DONE:
            return _DONE
        branch_current = branch.get("next_action_id")
        recorded = ((branch.get("actions") or {}).get(action_id) or {}).get("status")
        if action_id == branch_current and recorded not in {_DONE, "failed"}:
            return "running"
        return str(recorded or "pending")
    recorded = ((state.get("actions") or {}).get(action_id) or {}).get("status")
    if workflow_status == _DONE:
        return _DONE if recorded in {None, _DONE} else str(recorded)
    if action_id == current and recorded not in {_DONE, "failed"}:
        return "waiting" if workflow_status == "waiting" else "running"
    if recorded in {None, "pending"} and current in order and action_id in order:
        if order.index(action_id) < order.index(current):
            return "skipped"
    return str(recorded or "pending")


def _recent_events(path: Path, run_id: str) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - _TAIL_BYTES))
            tail = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = tail.splitlines()
    if len(tail.encode("utf-8")) >= _TAIL_BYTES and lines:
        lines = lines[1:]
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        if len(rows) >= _RECENT_EVENTS:
            break
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("run_id") != run_id:
            continue
        if event.get("kind") not in _EVENT_KINDS:
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        rows.append(
            {
                "kind": str(event.get("kind")),
                "workflow_id": str(event.get("workflow_id") or ""),
                "action_id": event.get("action_id"),
                "error": (str(event["error"])[:400] if event.get("error") else None),
                "ts": event.get("ts"),
                "key": data.get("key"),
                "agent_id": data.get("agent_id"),
                "tool_id": data.get("tool_id"),
                "reused": data.get("reused"),
            }
        )
    return rows


__all__ = ["RunProgressReader"]
