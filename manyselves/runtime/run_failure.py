"""Locate and classify why a Run stopped.

The classification only uses the plan's structure and the error text:

- ``temporary``: the Provider or the network failed (rate limit, timeout,
  connection, server error).  Running again is expected to work.
- ``model``: an Agent turn ended without a usable result (no submission,
  no progress, a result that kept failing its contract).  A fresh sample
  usually works, so running again is worth a bounded number of tries.
- ``cancelled``: someone stopped the Run.
- ``program``: a Tool or a workflow step failed.  Running again would fail
  the same way; it needs a code or input fix.

Nothing here knows a Capability.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from manyselves.kernel.workflow import ResolvedPlan

_TEMPORARY = re.compile(
    r"rate.?limit|\b429\b|请求数限制|timed? ?out|timeout|超时|connection|connect error|"
    r"network|网络|\b50[0-9]\b|server error|服务端错误|overloaded|unavailable|"
    r"stream (?:ended|closed|idle)|incomplete chunked|remote ?protocol",
    re.IGNORECASE,
)
_CANCELLED = re.compile(r"cancelled by user|run cancelled", re.IGNORECASE)


@dataclass(frozen=True)
class RunFailure:
    """Where a Run stopped and whether running it again may help."""

    path: list[str]
    action_kind: str | None
    error: str
    category: str
    retryable: bool

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def describe_failure(plan: ResolvedPlan, state: Mapping[str, Any]) -> RunFailure | None:
    """Return the deepest failed action of a failed Run, or None."""

    if str(state.get("status")) != "failed":
        return None
    found = _deepest(plan, state, [])
    if found is None:
        error = str(state.get("error") or "运行失败，未记录原因")
        return RunFailure([], None, error, *_classify(None, error))
    path, kind, error = found
    return RunFailure(path, kind, error, *_classify(kind, error))


def _classify(kind: str | None, error: str) -> tuple[str, bool]:
    if _CANCELLED.search(error):
        return "cancelled", False
    if _TEMPORARY.search(error):
        return "temporary", True
    if kind == "invoke_agent":
        return "model", True
    return "program", False


def _deepest(
    plan: ResolvedPlan, state: Mapping[str, Any], path: list[str]
) -> tuple[list[str], str | None, str] | None:
    actions = {action.id: action for action in plan.actions}
    for action_id, record in (state.get("actions") or {}).items():
        if not isinstance(record, Mapping) or record.get("status") != "failed":
            continue
        action = actions.get(action_id)
        kind = getattr(getattr(action, "kind", None), "value", None)
        here = [*path, action_id]
        child_plan = (
            plan.subworkflow_plans.get(getattr(action, "workflow", ""))
            if action is not None
            else None
        )
        if child_plan is not None:
            children: list[tuple[str, Mapping[str, Any]]] = []
            sub = (state.get("subworkflow_states") or {}).get(action_id)
            if isinstance(sub, Mapping):
                children.append(("", sub))
            for key, item in ((state.get("map_states") or {}).get(action_id) or {}).items():
                if isinstance(item, Mapping):
                    children.append((f"[{key}]", item))
            for suffix, child in children:
                if child.get("status") != "failed":
                    continue
                deeper = _deepest(child_plan, child, [*here[:-1], action_id + suffix])
                if deeper is not None:
                    return deeper
        return here, kind, str(record.get("error") or "")
    return None


__all__ = ["RunFailure", "describe_failure"]
