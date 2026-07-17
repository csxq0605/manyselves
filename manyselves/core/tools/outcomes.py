"""Normalized runtime outcomes for every tool invocation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolOutcome(BaseModel):
    """The semantic result of a tool call, independent of transport success."""

    status: Literal["ok", "failed", "blocked"] = "ok"
    terminal: bool = False
    result: Any = None
    error: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)


_TERMINAL_TOOLS = {
    "submit_result",
    "report_blocked",
    "run_reporting_workflow",
    "resume_reporting_workflow",
    "revise_reporting_workflow",
}
_FAILURE_STATUSES = {"error", "failed", "cancelled", "stopped_incomplete"}
_BLOCKED_STATUSES = {
    "blocked",
    "needs_decision",
    "needs_scope_expansion",
    "needs_user_decision",
}


def normalize_tool_outcome(value: Any, tool_name: str = "tool") -> ToolOutcome:
    """Convert legacy tool values into a truthful semantic outcome."""

    if isinstance(value, ToolOutcome):
        if tool_name in _TERMINAL_TOOLS and not value.terminal:
            return value.model_copy(update={"terminal": True})
        return value
    terminal = tool_name in _TERMINAL_TOOLS
    if not isinstance(value, dict):
        return ToolOutcome(result=value, terminal=terminal)

    raw_status = str(value.get("status", "ok")).strip().casefold()
    explicit_error = str(value.get("error") or "").strip() or None
    if raw_status in _BLOCKED_STATUSES:
        status = "blocked"
    elif raw_status in _FAILURE_STATUSES or explicit_error:
        status = "failed"
    else:
        status = "ok"

    refs: list[str] = []
    for key in ("artifact_refs", "output_paths"):
        raw_refs = value.get(key, [])
        if isinstance(raw_refs, (list, tuple)):
            refs.extend(str(ref) for ref in raw_refs if str(ref).strip())
    for key in ("artifact_ref", "result_path", "output_path"):
        ref = value.get(key)
        if ref:
            refs.append(str(ref))

    error = explicit_error
    if status != "ok" and error is None:
        error = str(value.get("reason") or value.get("message") or raw_status)
    return ToolOutcome(
        status=status,
        terminal=terminal,
        result=value,
        error=error,
        artifact_refs=list(dict.fromkeys(refs)),
    )


def canonical_terminal_message(outcome: ToolOutcome) -> str:
    """Return a deterministic final user-facing status for a terminal outcome."""

    payload = outcome.result if isinstance(outcome.result, dict) else {}
    if outcome.status == "ok":
        paths = payload.get("output_paths") or outcome.artifact_refs
        suffix = f" 输出：{', '.join(map(str, paths))}" if paths else ""
        return f"任务已完成并通过运行时校验。{suffix}".strip()
    if outcome.status == "blocked":
        return f"本次未交付：任务已阻塞。{outcome.error or '需要补充输入或作出决定。'}"
    return f"本次未交付：运行失败。{outcome.error or '请查看错误详情。'}"
