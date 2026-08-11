"""Agent Loop - core agent processing engine."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from .manager import LoopManager

from loguru import logger

from ...config.schema import AgentDefaults
from ...core.prompts import PromptLoader
from ...core.providers.base import (
    LLMProvider,
    LLMToolCall,
    ProviderRequestDisposition,
    provider_request_disposition,
)
from ...core.providers.base import Message as LLMMessage
from ...core.runtime_errors import (
    RuntimeErrorPolicy,
    classify_runtime_error,
    runtime_error_details,
)
from ...interfaces.types import (
    AgentId,
    AgentResponse,
    AgentStatus,
    AgentType,
    ApiDebugMessage,
    Error,
    Message,
    QueueUpdateMessage,
    ReportMessage,
    StatusChange,
    SystemNotice,
    TaskStatus,
    TaskUpdateMessage,
    ToolCallMessage,
    UserMessage,
    normalize_agent_id,
)
from ...interfaces.types import (
    ToolResult as ToolResultMsg,
)
from ...utils.agent_labels import get_agent_badge
from ...utils.editor_context import user_visible_content
from ..artifacts.gateway import ArtifactGateway, ArtifactGrant
from ..tools.manifest_tool import ManifestManager, ManifestTool
from ..tools.outcomes import (
    ToolOutcome,
    canonical_terminal_message,
    normalize_tool_outcome,
)
from ..tools.registry import ToolRegistry
from ..usage_ledger import UsageLedger
from .bus import MessageBus

# Tokens per char heuristic (cl100k_base averages ~0.25 tokens/char for code, ~0.3 for text)
_TOKENS_PER_CHAR = 0.3
_SAFETY_BUFFER = 1024  # Extra buffer for tool definitions and overhead
_MAX_PROVIDER_RETRIES = 2
_RESULT_PART_TOOLS = frozenset({"write_result_part", "write_result_parts"})
_RESULT_PART_COMPACTION_THRESHOLD = 256
_PERSISTED_RESULT_PART_MARKER = re.compile(
    r"^<persisted_result_part(?: [^>]*)?>$"
)

# Reaching a bounded tool slice is not a terminal Agent state. Reporting
# orchestration uses this signal to continue with the same durable identity.
AGENT_TURN_CONTINUATION_REQUIRED = "AGENT_TURN_CONTINUATION_REQUIRED"
AGENT_MAX_TOKENS_CONTINUATION_REQUIRED = (
    "AGENT_MAX_TOKENS_CONTINUATION_REQUIRED"
)

_CANCEL_REPORT_NEGATIONS = (
    "不要取消",
    "别取消",
    "不能取消",
    "不许取消",
    "do not cancel",
    "don't cancel",
    "do not stop",
    "don't stop",
)
_CANCEL_REPORT_REQUEST = re.compile(
    r"(?:^|[，。！？!?,;；\s])(?:请|立即|现在|马上|帮我)?"
    r"(?:取消|停止|终止)(?:当前|这个|该|正在运行的)?"
    r"(?:报告|报告任务|工作流|workflow|任务)"
    r"|^(?:please\s+)?(?:cancel|stop|terminate)\b.*\b(?:report|workflow|task)\b",
    re.IGNORECASE,
)

_REPORT_ROUTE_OBJECT = re.compile(
    r"(?:配电|安全评估|专家咨询)?报告"
    r"|(?:模块\s*2\.[1-5]|2\.[1-5]\s*模块)"
    r"|(?:模板(?:写作)?\s*(?:skill|技能))"
    r"|(?:markdown|md)\s*(?:转|转换|生成|导出).{0,8}(?:word|docx)"
    r"|(?:word|docx)\s*(?:报告|文件)",
    re.IGNORECASE,
)
_REPORT_ROUTE_ACTION = re.compile(
    r"开始|写作|撰写|编写|生成|重写|重新(?:分析|生成|写作|撰写)"
    r"|汇总|合并|聚合|渲染|转换|转成|导出|蒸馏|学习|更新",
    re.IGNORECASE,
)
_REPORT_ROUTE_DIAGNOSIS = re.compile(
    r"为什么|为何|原因|失败|报错|错误|调查|诊断|排查|状态|进度",
    re.IGNORECASE,
)
_REPORT_ROUTE_RESTART = re.compile(
    r"重新(?:开始|分析|生成|写作|撰写|运行)|重跑|再(?:生成|写|跑)一次",
    re.IGNORECASE,
)
_REPORT_ROUTE_FILE_TOOLS = frozenset(
    {
        "read",
        "inspect_document",
        "open_artifact",
        "open_tool_result",
        "search_text",
        "parse_pdf",
    }
)
_REPORTING_ENTRY_TOOLS = frozenset(
    {
        "run_reporting_workflow",
        "resume_reporting_workflow",
        "revise_reporting_workflow",
    }
)
_SIMPLE_REPORT_CONTINUATION = re.compile(
    r"^(?:请|麻烦|帮我)?"
    r"(?:继续|接着|恢复)"
    r"(?:在|从)?(?:这个|该|原(?:来的)?)?"
    r"(?:报告|报告任务|任务|run|运行)?(?:的)?"
    r"(?:断点|检查点)?(?:处)?"
    r"(?:继续|完成|运行|执行)?"
    r"[。！？!?\s]*$",
    re.IGNORECASE,
)
_RESUMABLE_REPORT_STATUSES = frozenset(
    {
        "failed",
        "cancelled",
        "blocked",
        "needs_decision",
        "needs_user_decision",
        "in_progress",
        "running",
        "interrupted",
    }
)
_UNVERIFIED_REPORT_OPERATION_CLAIM = re.compile(
    r"(?:已|已经|现已|正在|成功)(?:在后台)?"
    r"(?:启动|恢复|运行|重启|重新启动)"
    r"|(?:新(?:的)?\s*run|新运行).{0,20}(?:启动|运行)",
    re.IGNORECASE,
)
_REPORT_RUN_ID = re.compile(r"\breport-[A-Za-z0-9_-]+\b")
_EVIDENCE_DECISION_PATTERNS = {
    "draft": re.compile(
        r"(?<![A-Za-z0-9_])draft(?![A-Za-z0-9_])"
        r"|保留不确定性.{0,12}起草|按不确定性起草|继续起草",
        re.IGNORECASE,
    ),
    "skip": re.compile(r"\bskip\b|跳过|保留目录.{0,12}未评估", re.IGNORECASE),
    "stop": re.compile(r"\bstop\b|停止|终止", re.IGNORECASE),
    "supplement": re.compile(r"\bsupplement\b|补充资料|补录|我补充", re.IGNORECASE),
}


def _direct_user_content(message: UserMessage | None) -> str:
    """Return the user's command body without an editor-context wrapper."""

    if message is None or str(getattr(message, "source", "user") or "user") != "user":
        return ""
    return user_visible_content(str(getattr(message, "content", "") or "")).strip()


def _is_explicit_report_cancel_request(message: UserMessage | None) -> bool:
    """Return True only for a direct end-user instruction to cancel a report."""

    content = _direct_user_content(message)
    if not content:
        return False
    folded = content.casefold()
    if content == "/stop":
        return True
    if any(negation in folded for negation in _CANCEL_REPORT_NEGATIONS):
        return False
    return bool(_CANCEL_REPORT_REQUEST.search(content))


def _requires_reporting_workflow_route(message: UserMessage | None) -> bool:
    """Identify direct report operations that Main must route before reading files."""

    content = _direct_user_content(message)
    if not content:
        return False
    if _REPORT_ROUTE_DIAGNOSIS.search(content) and not _REPORT_ROUTE_RESTART.search(
        content
    ):
        return False
    return bool(
        _REPORT_ROUTE_OBJECT.search(content) and _REPORT_ROUTE_ACTION.search(content)
    )


def _report_workflow_terminal_payload(
    message: UserMessage | None,
) -> dict[str, Any] | None:
    """Decode a controller-owned terminal message without asking Main to infer it."""

    if message is None or str(getattr(message, "source", "") or "") != "report-workflow":
        return None
    try:
        payload = json.loads(str(getattr(message, "content", "") or ""))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    run_id = str(payload.get("run_id", "") or "")
    status = str(payload.get("status", "") or "")
    if not run_id.startswith("report-") or not status:
        return None
    return payload


def _canonical_failed_report_response(message: UserMessage | None) -> str | None:
    """Return the non-agentic response for a failed workflow terminal event."""

    payload = _report_workflow_terminal_payload(message)
    if payload is None or payload.get("status") != "failed":
        return None
    run_id = str(payload["run_id"])
    error = str(payload.get("error") or "未提供错误详情")
    return (
        f"报告任务 {run_id} 已失败：{error}\n\n"
        f"本轮没有启动新运行。若要从已保存断点继续，请明确要求恢复 {run_id}。"
    )


def _is_simple_report_continuation(message: UserMessage | None) -> bool:
    """Match only a continuation command that carries no new business facts."""

    return bool(_SIMPLE_REPORT_CONTINUATION.fullmatch(_direct_user_content(message)))


def _explicit_report_continuation_run_id(
    message: UserMessage | None,
) -> str | None:
    """Return one explicitly named run for a short resume/continue instruction."""

    content = _direct_user_content(message)
    if not content:
        return None
    if not re.search(r"继续|接着|恢复|断点|检查点", content):
        return None
    run_ids = _REPORT_RUN_ID.findall(content)
    if len(set(run_ids)) != 1 or len(content) > 120:
        return None
    if re.search(r"补充|新增|修改|改成|人工确认|事实|数据", content):
        return None
    return run_ids[0]


def _is_explicit_evidence_decision(
    message: UserMessage | None, action: str | None
) -> bool:
    """Require the current end-user message to select the evidence action."""

    content = _direct_user_content(message)
    pattern = _EVIDENCE_DECISION_PATTERNS.get(str(action or "").casefold())
    if pattern is None or not content:
        return False
    return bool(pattern.search(content))


@dataclass
class _LoopLLMResponse:
    content: str
    tool_calls: list
    thinking: str | None = None
    usage: dict[str, int] | None = None
    stop_reason: str | None = None
    streamed: bool = False
    request_metrics: dict[str, Any] | None = None
    ttft_ms: int | None = None
    provider_active_ms: int | None = None


@dataclass
class _ProgressMonitor:
    """Detect repeated rounds that add no new observable tool result."""

    replan_after: int = 1
    _last_fingerprint: str | None = None
    _stagnant_rounds: int = 0

    def observe(self, results: list[str]) -> str | None:
        normalized = sorted(str(result).strip() for result in results if str(result).strip())
        fingerprint = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if fingerprint != self._last_fingerprint:
            self._last_fingerprint = fingerprint
            self._stagnant_rounds = 0
            return None
        self._stagnant_rounds += 1
        if self._stagnant_rounds >= self.replan_after:
            self._stagnant_rounds = 0
            return "replan"
        return None


def _remember_persisted_result_part_content(
    tool_call: LLMToolCall,
    contents: dict[str, str],
) -> None:
    """Keep exact persisted prose available for defensive history recovery."""

    if tool_call.name not in _RESULT_PART_TOOLS:
        return
    arguments = tool_call.arguments or {}
    if tool_call.name == "write_result_part":
        items = [arguments]
    else:
        items = arguments.get("parts", arguments.get("items", []))
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if (
            not isinstance(content, str)
            or len(content) < _RESULT_PART_COMPACTION_THRESHOLD
        ):
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        contents[digest] = content


def _rehydrate_persisted_result_part_call(
    tool_call: LLMToolCall,
    contents: dict[str, str],
    workspace: Path | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
) -> LLMToolCall:
    """Restore only an authentic historical marker before tool execution.

    Provider-visible schemas forbid sending these history-only placeholders.
    This recovery path exists solely as a defensive compatibility boundary:
    the exact digest and length must match cached prose or the marker's existing
    same-run, same-task draft. A fabricated, edited, cross-task, or missing-file
    marker remains unresolved for deterministic correction by AgentLoop.
    """

    def marker_field(marker: str, name: str) -> str | None:
        match = re.search(rf"(?:^| ){re.escape(name)}=([^ >]+)(?: |>)", marker)
        return match.group(1) if match is not None else None

    def load_marker_artifact(
        marker: str,
        *,
        expected_part_id: str | None = None,
    ) -> str | None:
        """Read only a same-workspace result-part draft named by its marker."""

        if workspace is None:
            return None
        artifact_ref = marker_field(marker, "artifact_ref")
        if not artifact_ref:
            return None
        relative = Path(artifact_ref)
        if relative.is_absolute():
            return None
        workspace_root = workspace.resolve()
        target = (workspace_root / relative).resolve()
        if not target.is_relative_to(workspace_root) or not target.is_file():
            return None
        target_relative = target.relative_to(workspace_root)
        parts = target_relative.parts
        if (
            len(parts) < 5
            or parts[:2] != ("Work", "runs")
            or "drafts" not in parts
            or target.suffix.casefold() != ".md"
        ):
            return None
        if run_id is not None and task_id is not None:
            expected_root = (
                workspace_root / "Work" / "runs" / run_id / "drafts" / task_id
            ).resolve()
            if not expected_root.is_relative_to(workspace_root):
                return None
            if not target.is_relative_to(expected_root):
                return None
        marker_part_id = marker_field(marker, "part_id")
        if (
            expected_part_id is not None
            and marker_part_id is not None
            and marker_part_id != expected_part_id
        ):
            return None
        part_id = expected_part_id or marker_part_id
        if part_id is not None and target.name != f"{part_id}.md":
            return None
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        if "<persisted_result_part" in content.casefold():
            return None
        return content

    def remember(content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        contents[digest] = content
        return content

    def restore(value: Any) -> Any:
        if isinstance(value, list):
            return [restore(item) for item in value]
        if isinstance(value, dict):
            return {key: restore(item) for key, item in value.items()}
        if not isinstance(value, str):
            return value
        match = _PERSISTED_RESULT_PART_MARKER.fullmatch(value.strip())
        if match is None:
            return value
        digest = marker_field(value, "sha256")
        characters = marker_field(value, "characters")
        if (
            digest is None
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or characters is None
            or re.fullmatch(r"[1-9][0-9]*", characters) is None
        ):
            return value
        content = contents.get(digest)
        marker_part_id = marker_field(value, "part_id")
        if content is None:
            artifact_content = load_marker_artifact(
                value,
                expected_part_id=marker_part_id,
            )
            if (
                artifact_content is not None
                and len(artifact_content) == int(characters)
                and hashlib.sha256(artifact_content.encode("utf-8")).hexdigest()
                == digest
            ):
                content = remember(artifact_content)
        if content is None or len(content) != int(characters):
            return value
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
            return value
        return content

    def restore_result_part(item: Any) -> Any:
        if not isinstance(item, dict):
            return restore(item)
        return {key: restore(value) for key, value in item.items()}

    arguments = dict(tool_call.arguments or {})
    if tool_call.name == "write_result_part":
        restored_arguments = restore_result_part(arguments)
    elif tool_call.name == "write_result_parts":
        argument_key = "parts" if isinstance(arguments.get("parts"), list) else "items"
        items = arguments.get(argument_key)
        restored_arguments = {key: restore(value) for key, value in arguments.items()}
        if isinstance(items, list):
            restored_arguments[argument_key] = [
                restore_result_part(item) for item in items
            ]
    else:
        restored_arguments = restore(arguments)
    return LLMToolCall(
        id=tool_call.id,
        name=tool_call.name,
        arguments=restored_arguments,
    )


def _unresolved_persisted_result_part_ids(tool_call: LLMToolCall) -> list[str]:
    """Return result-part ids whose content still contains an internal marker."""

    if tool_call.name not in _RESULT_PART_TOOLS:
        return []
    arguments = tool_call.arguments or {}
    if tool_call.name == "write_result_part":
        items = [arguments]
    else:
        items = arguments.get("parts", arguments.get("items", []))
    if not isinstance(items, list):
        return []
    affected: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if (
            isinstance(content, str)
            and "<persisted_result_part" in content.casefold()
        ):
            part_id = str(item.get("part_id") or "").strip()
            affected.append(part_id or "<unknown>")
    return list(dict.fromkeys(affected))


def _redact_unresolved_persisted_result_part_call(
    tool_call: LLMToolCall,
) -> LLMToolCall:
    """Sanitize rejected marker text without creating a malformed history call."""

    rejected_content = (
        "[Rejected internal history marker. Regenerate complete reader-visible prose "
        "before retrying this part.]"
    )
    arguments = dict(tool_call.arguments or {})
    if tool_call.name == "write_result_part":
        content = arguments.get("content")
        if (
            isinstance(content, str)
            and "<persisted_result_part" in content.casefold()
        ):
            arguments["content"] = rejected_content
    elif tool_call.name == "write_result_parts":
        argument_key = "parts" if isinstance(arguments.get("parts"), list) else "items"
        items = arguments.get(argument_key)
        if isinstance(items, list):
            redacted_items = []
            for item in items:
                if not isinstance(item, dict):
                    redacted_items.append(item)
                    continue
                redacted = dict(item)
                content = redacted.get("content")
                if (
                    isinstance(content, str)
                    and "<persisted_result_part" in content.casefold()
                ):
                    redacted["content"] = rejected_content
                redacted_items.append(redacted)
            arguments[argument_key] = redacted_items
    return LLMToolCall(
        id=tool_call.id,
        name=tool_call.name,
        arguments=arguments,
    )


class _PersistedResultPartCorrection(RuntimeError):
    """A model copied an internal history placeholder instead of prose."""

    def __init__(self, tool_call: LLMToolCall, affected_part_ids: list[str]):
        self.tool_call = tool_call
        self.payload = {
            "status": "correction_required",
            "accepted": False,
            "problem": (
                "One or more result parts supplied an internal history placeholder "
                "instead of complete reader-visible prose."
            ),
            "affected_part_ids": affected_part_ids,
            "next_action": "list_result_parts_then_write_missing_or_rewrite_parts_once",
            "do_not_repeat_same_shape": True,
            "repair_instruction": (
                "Call list_result_parts once. Leave every ready part unchanged. "
                "For each missing or assigned rewrite part, call write_result_part "
                "with the complete intended reader-visible "
                "prose and evidence_ids. Never copy, construct, or submit an "
                "internal history placeholder or any digest from prior messages."
            ),
        }
        super().__init__(self.payload["problem"])


class _ToolInputCorrection(RuntimeError):
    """Return schema-oriented feedback without turning a repairable call into ERROR."""

    def __init__(
        self,
        tool_call: LLMToolCall,
        *,
        required_argument_names: list[str],
        missing_argument_names: list[str],
    ):
        self.tool_call = tool_call
        received_argument_names = sorted((tool_call.arguments or {}).keys())
        self.payload = {
            "status": "correction_required",
            "accepted": False,
            "tool_name": tool_call.name,
            "problem": (
                f"Tool input is incomplete: missing required arguments "
                f"{missing_argument_names}."
            ),
            "required_argument_names": required_argument_names,
            "missing_argument_names": missing_argument_names,
            "received_argument_names": received_argument_names,
            "next_action": f"call_{tool_call.name}_once_with_complete_arguments",
            "do_not_repeat_same_shape": True,
            "repair_instruction": (
                f"Call {tool_call.name} once with one native argument object containing "
                f"every required field: {required_argument_names}. Preserve already-valid "
                "values, fill only the missing fields, and do not retry the same incomplete "
                "argument shape. For durable report prose, call list_result_parts first and "
                "leave every ready part unchanged."
            ),
        }
        super().__init__(self.payload["problem"])


class _ProviderAttemptError(RuntimeError):
    """One provider attempt failed, optionally after visible output."""

    def __init__(self, original: BaseException, *, partial_output: bool):
        super().__init__(str(original))
        self.original = original
        self.partial_output = partial_output
        self.attempt_disposition = (
            ProviderRequestDisposition.ACCEPTED_OR_UNKNOWN
            if partial_output
            else provider_request_disposition(original)
        )


class _ProviderRequestError(RuntimeError):
    """A provider request cannot be retried again."""

    def __init__(
        self,
        original: BaseException,
        policy: RuntimeErrorPolicy,
        *,
        attempts: int,
        partial_output: bool,
        ambiguous: bool,
        attempt_disposition: ProviderRequestDisposition,
    ):
        self.original = original
        self.policy = policy
        self.attempts = attempts
        self.had_partial_output = partial_output
        self.ambiguous = ambiguous
        # ReportingService historically uses partial_output as its durable
        # ambiguity signal. Preserve that contract for accepted-or-unknown
        # requests even when no token was observed locally.
        self.partial_output = partial_output or ambiguous
        self.attempt_disposition = attempt_disposition.value
        retry_text = f"，已自动重试{attempts - 1}次仍失败" if attempts > 1 else ""
        partial_text = (
            "；响应已产生部分内容，为避免重复输出未自动重试"
            if partial_output
            else ""
        )
        ambiguous_text = (
            "；请求可能已被服务端接受，状态不确定，未自动重试"
            if ambiguous and not partial_output
            else ""
        )
        super().__init__(
            f"{policy.title}{retry_text}{partial_text}{ambiguous_text}：{original}"
        )


def _estimate_tokens(messages: list[LLMMessage]) -> int:
    """Rough token count — ~4 chars per token, plus per-message overhead."""
    total = 0
    for m in messages:
        total += 4  # Per-message overhead
        if m.content:
            total += int(len(m.content) * _TOKENS_PER_CHAR)
        if getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                total += 20  # Tool call overhead
                if hasattr(tc, "arguments") and tc.arguments:
                    total += int(len(str(tc.arguments)) * _TOKENS_PER_CHAR)
        if getattr(m, "thinking", None) and m.thinking:
            total += int(len(m.thinking) * _TOKENS_PER_CHAR)
    return total


def _sanitize_provider_visible_text(value: str) -> str:
    """Remove retired history tokens before any payload reaches a Provider.

    Old durable transcripts can legitimately contain the token. It remains in
    the lossless local trace, but showing its spelling back to a model turns an
    internal storage protocol into an in-context example.
    """

    sanitized = re.sub(
        r"<persisted_result_part(?: [^>]*)?>",
        "[retired internal history token omitted]",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"persisted_result_part",
        "retired_internal_history_token",
        sanitized,
        flags=re.IGNORECASE,
    )


def _sanitize_provider_visible_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_provider_visible_text(value)
    if isinstance(value, dict):
        return {
            _sanitize_provider_visible_text(str(key)): _sanitize_provider_visible_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_provider_visible_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_provider_visible_value(item) for item in value)
    return value


def _sanitize_provider_messages(
    messages: list[LLMMessage],
) -> list[LLMMessage]:
    """Return a protocol-equivalent Provider view with legacy tokens redacted."""

    return [
        LLMMessage(
            role=message.role,
            content=_sanitize_provider_visible_text(message.content or ""),
            tool_calls=(
                [
                    LLMToolCall(
                        id=_sanitize_provider_visible_text(call.id),
                        name=_sanitize_provider_visible_text(call.name),
                        arguments=_sanitize_provider_visible_value(call.arguments),
                    )
                    for call in message.tool_calls
                ]
                if message.tool_calls is not None
                else None
            ),
            tool_call_id=(
                _sanitize_provider_visible_text(message.tool_call_id)
                if message.tool_call_id is not None
                else None
            ),
            is_tool_result=message.is_tool_result,
            thinking=(
                _sanitize_provider_visible_text(message.thinking)
                if message.thinking is not None
                else None
            ),
            cache_control=message.cache_control,
        )
        for message in messages
    ]


def _estimate_response_tokens(response: Any) -> int:
    """Estimate all generated payload, including structured tool arguments."""

    content = str(getattr(response, "content", "") or "")
    thinking = str(getattr(response, "thinking", "") or "")
    tool_calls = [
        {
            "id": getattr(call, "id", None),
            "name": getattr(call, "name", None),
            "arguments": getattr(call, "arguments", None),
        }
        for call in (getattr(response, "tool_calls", None) or [])
    ]
    structured = (
        json.dumps(
            tool_calls,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        if tool_calls
        else ""
    )
    return int((len(content) + len(thinking) + len(structured)) * _TOKENS_PER_CHAR)


def _trim_messages_to_budget(
    messages: list[LLMMessage],
    context_window: int = 128000,
    max_output: int = 4096,
) -> list[LLMMessage]:
    """Trim oldest messages to stay within context budget.

    Inspired by nanobot's SnipHistory: walk from the end, keep messages
    that fit within the budget, preserve user-turn boundaries.
    """
    budget = context_window - max_output - _SAFETY_BUFFER
    estimated = _estimate_tokens(messages)
    if estimated <= budget:
        return messages

    logger.info(
        "Context auto-compact: {} tokens exceed budget {} → trimming",
        estimated,
        budget,
    )

    # Always keep system message (index 0)
    system_msg = messages[0]
    rest = messages[1:]

    # Walk backwards, accumulate tokens
    kept = []
    kept_tokens = 0
    for m in reversed(rest):
        mt = _estimate_tokens([m])
        if kept_tokens + mt <= budget:
            kept.insert(0, m)
            kept_tokens += mt
        else:
            break

    # Ensure the boundary is legal.
    # 1. Strip orphan 'tool' messages whose 'assistant(tool_calls)' was trimmed.
    # 2. Strip orphan 'assistant' messages whose preceding 'user' was trimmed.
    # 3. The first real message after system must be 'user'.
    # 4. Anthropic-style tool results (role="user", is_tool_result=True) whose
    #    'assistant(tool_calls)' was trimmed.
    while kept:
        role = kept[0].role
        is_tool_result = getattr(kept[0], "is_tool_result", False)
        if role == "tool":
            logger.debug("Stripping orphan tool message at trim boundary")
            kept.pop(0)
        elif role == "assistant":
            logger.debug("Stripping orphan assistant message at trim boundary")
            kept.pop(0)
        elif role == "user" and is_tool_result:
            # Anthropic-style orphan: tool result disguised as user message.
            # Without its preceding assistant(tool_calls), this would cause
            # an API error (tool result without tool_use).
            logger.debug("Stripping orphan user/tool_result message at trim boundary")
            kept.pop(0)
        else:
            break

    # Always prepend system prompt
    result = [system_msg] + kept

    logger.info(
        "Context compacted: {} → {} messages ({} → ~{} tokens)",
        len(messages),
        len(result),
        estimated,
        _estimate_tokens(result),
    )
    return result


def _compact_messages_for_working_memory(
    messages: list[LLMMessage],
    *,
    target_tokens: int = 36000,
) -> list[LLMMessage]:
    """Keep a focused working set and replace older history with a checkpoint."""

    if not messages or _estimate_tokens(messages) <= target_tokens:
        return messages

    system_msg = messages[0]
    history = messages[1:]
    objective = ""
    for message in history:
        if (
            message.role != "user"
            or getattr(message, "is_tool_result", False)
            or not message.content
        ):
            continue
        previous = re.search(
            r"<original_objective>(.*?)</original_objective>",
            message.content,
            flags=re.DOTALL,
        )
        objective = (
            previous.group(1)[:1200]
            if previous is not None
            and "<working_memory_checkpoint>" in message.content
            else message.content[:1200]
        )
        break
    combined = "\n".join(message.content or "" for message in history)

    # Prefer references mentioned most recently.  A sorted set used to discard
    # recency and could evict the exact finding/result-part ids needed to finish
    # the active task while retaining older, alphabetically earlier ids.
    reference_candidates = (
        re.findall(
            r"\b(?:Work|Outputs|Knowledge)/[^\s<>'\"，。；]+",
            combined,
        )
        + re.findall(r"\b(?:E|R|W)-[A-Za-z0-9_.-]+", combined)
    )
    references = list(dict.fromkeys(reversed(reference_candidates)))[:120]
    references.reverse()
    identifier_candidates = re.findall(
        r"\b(?:E|R|W|RN|SI|M|X|F)-[A-Za-z0-9_.-]+",
        combined,
    )
    retained_identifiers = list(
        dict.fromkeys(reversed(identifier_candidates))
    )[:120]
    retained_identifiers.reverse()
    shared_memory_refs = [
        ref for ref in references if ref.endswith("-evidence-memory.json")
    ]
    latest_tool_state = _latest_compaction_tool_state(history)
    checkpoint_content = (
        "<working_memory_checkpoint>\n"
        f"<original_objective>{objective}</original_objective>\n"
        f"<retained_references>{' '.join(references)}</retained_references>\n"
        f"<retained_identifiers>{' '.join(retained_identifiers)}</retained_identifiers>\n"
        f"<shared_memory_refs>{' '.join(shared_memory_refs)}</shared_memory_refs>\n"
        + (
            f"<latest_tool_state>{latest_tool_state}</latest_tool_state>\n"
            if latest_tool_state
            else ""
        )
        + "Older tool transcripts were persisted locally. Continue from the recent "
        "messages. Reuse shared memory before searching or reopening source records. "
        "The complete removed transcript remains available losslessly through "
        "checkpoint_ref; call open_tool_result only when a needed detail is absent "
        "from the retained messages. "
        "Never recreate an older write merely because it is absent here: call "
        "list_result_parts once and write only missing or explicitly assigned rewrite "
        "parts. Current tool schemas, not this checkpoint, define argument shapes.\n"
        "</working_memory_checkpoint>"
    )
    checkpoint = LLMMessage(role="user", content=checkpoint_content)
    fixed_tokens = _estimate_tokens([system_msg, checkpoint])
    recent_budget = max(0, target_tokens - fixed_tokens)

    kept: list[LLMMessage] = []
    kept_tokens = 0
    # Tool use is one protocol object: assistant(tool_calls) plus every matching
    # tool result.  Retaining or removing individual messages can manufacture an
    # invalid example and make the next model call imitate missing arguments.
    # Compact only complete atomic units.
    for unit in reversed(_atomic_history_units(history)):
        if any(
            message.role == "user"
            and "<working_memory_checkpoint>" in (message.content or "")
            for message in unit
        ):
            continue
        unit_tokens = _estimate_tokens(unit)
        if kept_tokens + unit_tokens > recent_budget:
            break
        kept[0:0] = unit
        kept_tokens += unit_tokens

    return [system_msg, checkpoint, *kept]


def _atomic_history_units(history: list[LLMMessage]) -> list[list[LLMMessage]]:
    """Group provider history without splitting a tool call from its results."""

    units: list[list[LLMMessage]] = []
    index = 0
    while index < len(history):
        message = history[index]
        calls = list(getattr(message, "tool_calls", None) or [])
        if message.role != "assistant" or not calls:
            # Never retain an orphan result.  It remains available in the exact
            # durable transcript written by ``_compact_working_memory``.
            if message.role == "tool" or getattr(message, "is_tool_result", False):
                index += 1
                continue
            units.append([message])
            index += 1
            continue

        call_ids = [str(getattr(call, "id", "") or "") for call in calls]
        expected_ids = set(call_ids)
        calls_are_valid = (
            all(call_ids)
            and len(call_ids) == len(calls)
            and len(expected_ids) == len(call_ids)
        )
        unit = [message]
        seen_ids: set[str] = set()
        scan = index + 1
        while scan < len(history):
            candidate = history[scan]
            if not (
                candidate.role == "tool"
                or getattr(candidate, "is_tool_result", False)
            ):
                break
            result_id = str(getattr(candidate, "tool_call_id", "") or "")
            # A result for another call, a missing id, or a duplicate result is
            # an orphan protocol object.  Stop the unit before it; the outer
            # loop will discard that result while preserving a preceding
            # complete exchange, if any.
            if (
                not result_id
                or result_id not in expected_ids
                or result_id in seen_ids
            ):
                break
            unit.append(candidate)
            seen_ids.add(result_id)
            scan += 1

        # An incomplete exchange is never shown back to a Provider as a valid
        # example.  It is still losslessly present in the persisted checkpoint.
        if calls_are_valid and expected_ids.issubset(seen_ids):
            units.append(unit)
        index = scan
    return units


def _latest_compaction_tool_state(history: list[LLMMessage]) -> str:
    """Retain compact typed progress, not arbitrary old tool prose."""

    call_names: dict[str, str] = {}
    for message in history:
        for call in getattr(message, "tool_calls", None) or []:
            call_id = str(getattr(call, "id", "") or "")
            if call_id:
                call_names[call_id] = str(getattr(call, "name", "") or "")

    retained_keys = {
        "status",
        "accepted",
        "complete",
        "next_action",
        "part_id",
        "ready_part_ids",
        "missing_part_ids",
        "rewrite_part_ids",
        "do_not_rewrite_part_ids",
        "affected_part_ids",
        "required_synthesis_input_ids",
        "correction_state_ref",
        "result_path",
    }
    latest: dict[str, dict[str, Any]] = {}
    for message in history:
        if not getattr(message, "is_tool_result", False):
            continue
        try:
            payload = json.loads(message.content or "")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        compact = {key: payload[key] for key in retained_keys if key in payload}
        if not compact:
            continue
        tool_name = call_names.get(str(message.tool_call_id or ""), "tool")
        latest[tool_name] = compact

    if not latest:
        return ""
    rendered = json.dumps(
        latest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    # The retired marker token must never become a model-visible recovery hint.
    rendered = re.sub(
        r"persisted_result_part",
        "internal_history_placeholder",
        rendered,
        flags=re.IGNORECASE,
    )
    return rendered[:6000]


def _tool_required_args(tool_registry: ToolRegistry, tool_name: str, tool: Any | None = None) -> list[str]:
    """Return required argument names from the registered tool schema or signature."""
    for definition in tool_registry.get_definitions():
        if definition.get("name") == tool_name:
            schema = definition.get("input_schema") or {}
            required = schema.get("required") or []
            return [str(name) for name in required]
    if tool is not None:
        try:
            sig = inspect.signature(tool)
            return [
                name
                for name, param in sig.parameters.items()
                if (
                    name != "self"
                    and param.kind
                    not in (
                        inspect.Parameter.VAR_POSITIONAL,
                        inspect.Parameter.VAR_KEYWORD,
                    )
                    and param.default is inspect.Parameter.empty
                )
            ]
        except (TypeError, ValueError):
            return []
    return []


class AgentLoop:
    """Agent loop for processing user messages and generating responses.

    All GUI communication goes through the MessageBus as typed messages.
    The GUI subscribes to the bus and handles them.
    """

    def __init__(
        self,
        agent_type: AgentId | AgentType,
        workspace: Path,
        tools: ToolRegistry,
        bus: MessageBus,
        config: AgentDefaults,
        llm_provider: LLMProvider,
        prompt_loader: PromptLoader | None = None,
        loop_manager: LoopManager | None = None,
        manifest_manager: ManifestManager | None = None,
        task_board=None,
        system_prompt: str | None = None,
        usage_run_id: str | None = None,
        usage_task_id: str | None = None,
        artifact_gateway: ArtifactGateway | None = None,
        before_provider_attempt: Callable[[], Awaitable[None]] | None = None,
        provider_attempt_observer: (
            Callable[
                [list[LLMMessage], list[dict] | None, str, int],
                Awaitable[None],
            ]
            | None
        ) = None,
        provider_attempt_record_observer: (
            Callable[[dict[str, Any]], Awaitable[None]] | None
        ) = None,
    ):
        """Initialize agent loop.

        Args:
            agent_type: Type of this agent.
            workspace: Project workspace directory.
            tools: Tool registry for this agent.
            bus: Message bus for communication.
            config: Agent configuration.
            llm_provider: LLM provider for generating responses.
            prompt_loader: Optional custom PromptLoader instance.
            loop_manager: Optional LoopManager reference for coordination.
        """
        self.agent_id = normalize_agent_id(agent_type)
        # Backward-compatible attribute name; runtime identity is always a string.
        self.agent_type = self.agent_id
        self.workspace = Path(workspace).resolve()
        self.tools = tools
        self.bus = bus
        self.config = config
        self.llm_provider = llm_provider
        self._loop_manager = loop_manager
        self._manifest_manager = manifest_manager
        # Shared task board — used by the loop guards (must-report, main-blocked).
        self._task_board = task_board

        self._prompt_loader = prompt_loader or PromptLoader()
        self._system_prompt_override = system_prompt
        self._cached_system_prompt: str | None = None
        self._cached_system_prompt_signature: tuple[Any, ...] | None = None

        self._status = AgentStatus.IDLE
        self._turn_complete_event = asyncio.Event()
        self._turn_complete_event.set()
        self._running = False
        self._processing_task: asyncio.Task | None = None
        self._active_tool_task: asyncio.Task | None = None
        self._message_queue: asyncio.Queue[UserMessage] = asyncio.Queue()
        self._current_message: UserMessage | None = None
        # Set True when this agent emits a ReportMessage during the current
        # turn. The turn-end guard checks it to enforce that a Main-dispatched
        # task ends with an explicit report.
        self._turn_reported: bool = False
        # Guard retry counters (reset each turn in _process_message).
        self._report_retries: int = 0
        self._main_block_retries: int = 0
        self._conversation_history: list[LLMMessage] = []
        self._persisted_result_part_contents: dict[str, str] = {}
        self._current_session_id: str | None = None
        self._manifest_dirty = False
        self._cancel_event = asyncio.Event()
        self._consecutive_errors = 0
        self._progress_monitor = _ProgressMonitor()
        self.usage_run_id = usage_run_id
        self.usage_task_id = usage_task_id
        self.usage_context_manifest_ref: str | None = None
        self.usage_provider_call_id: str | None = None
        self.before_provider_attempt = before_provider_attempt
        self.provider_attempt_observer = provider_attempt_observer
        self.provider_attempt_record_observer = provider_attempt_record_observer
        self._usage_totals = {"input_tokens": 0, "output_tokens": 0}
        self._last_usage_record: dict[str, Any] | None = None
        self._usage_queue_wait_ms = 0
        self._usage_context_build_ms = 0
        self._usage_tool_time_ms = 0
        self._terminal_outcome: ToolOutcome | None = None
        self._terminal_tool_name: str | None = None
        self._buffered_main_response: str = ""
        self.artifact_gateway = artifact_gateway or ArtifactGateway(
            self.workspace,
            ArtifactGrant("main", "main", str(self.agent_type), "main"),
        )

        # Debug mode
        self._debug_mode = False
        self._bus_callback = self._handle_user_message

        # Task update batching — coalesce rapid-fire notifications into one queue message
        self._task_batch: list[str] = []
        self._task_batch_timer: asyncio.TimerHandle | None = None

        # Subscribe to user messages for this agent type
        self.bus.subscribe(UserMessage, self._bus_callback)
        # Subscribe to task updates
        self.bus.subscribe(TaskUpdateMessage, self._handle_task_update)
        # All agents track their own report emissions (turn-reported flag).
        self.bus.subscribe(ReportMessage, self._handle_report_message)
        # Manifest tool is only for sub-agents
        if self.agent_id != "main" and self._manifest_manager is not None:
            self.tools.register(ManifestTool(self._manifest_manager, self._get_agent_type_str()))

    @property
    def status(self) -> AgentStatus:
        """Get current agent status."""
        return self._status

    def reset_working_memory_for_typed_task(self) -> None:
        """Keep the durable identity while dropping prior task prompt/history replay."""

        if self._status not in {AgentStatus.IDLE, AgentStatus.ERROR}:
            raise RuntimeError("cannot reset working memory while the Agent is active")
        self._conversation_history = []
        self._persisted_result_part_contents = {}

    async def wait_until_turn_complete(self) -> None:
        """Wait until the current queued provider/tool turn has fully finalized."""

        await self._turn_complete_event.wait()

    async def start(self) -> None:
        """Start the agent loop."""
        if self._running:
            return

        self._running = True
        logger.info("Starting agent loop for {}", self.agent_type)
        await self._publish_queue_update()

        # Start processing task
        self._processing_task = asyncio.create_task(self._process_loop())

    async def stop(self) -> None:
        """Stop the agent loop."""
        if not self._running:
            return

        self._running = False
        active_tool = self._active_tool_task
        if active_tool is not None and not active_tool.done():
            active_tool.cancel()
        self.bus.unsubscribe(UserMessage, self._bus_callback)
        self.bus.unsubscribe(TaskUpdateMessage, self._handle_task_update)
        self.bus.unsubscribe(ReportMessage, self._handle_report_message)
        if self._processing_task is not None:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
            self._processing_task = None
        logger.info("Stopping agent loop for {}", self.agent_type)

    def cancel_current(self) -> None:
        """Cancel the currently processing message (if any)."""
        self._cancel_event.set()
        active_tool = self._active_tool_task
        if active_tool is not None and not active_tool.done():
            active_tool.cancel()
        logger.info("Cancel requested for agent {}", self.agent_type)

    async def _process_loop(self) -> None:
        """Main processing loop for messages."""
        while self._running:
            try:
                # Wait for next message with timeout so stop() can break out
                try:
                    message = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                self._current_message = message
                await self._publish_queue_update()
                await self._process_message(message)

            except Exception as e:
                logger.error("Error in agent loop for {}: {}", self.agent_type, str(e))
                await self.bus.publish(
                    Error(
                        source=str(self.agent_type),
                        message=str(e),
                        **self._active_workflow_correlation(),
                    )
                )

    async def _handle_user_message(self, message: Message) -> None:
        """Handle user message from bus.

        In debug mode, only direct user input is accepted;
        main-agent coordination commands are silently dropped.

        Args:
            message: User message.
        """
        if not isinstance(message, UserMessage):
            return

        # Check if message is for this agent
        if message.agent_type != self.agent_type:
            return

        # Debug mode: ignore main-agent coordination
        if self._debug_mode and message.source == "main_agent":
            logger.debug(
                "Debug mode: ignoring main-agent message for {}: {}",
                self.agent_type,
                message.content[:80],
            )
            return

        await self._message_queue.put(message)
        await self._publish_queue_update()

    async def _handle_task_update(self, message: Message) -> None:
        """Handle TaskUpdateMessage from the message bus.

        Task notifications are always delivered, even in debug mode.
        They are wrapped into a UserMessage(source="system") for LLM processing.

        Notification text is perspective-aware:
        - Agent as source (waiting): "等待 <target>：<description>"
        - Agent as target (todo): "完成 <source> 的任务：<description>"
        - Main agent: "<source> 等待 <target> 的 <description>"
        """
        if not isinstance(message, TaskUpdateMessage):
            return

        src_val = normalize_agent_id(message.source_agent)
        tgt_val = normalize_agent_id(message.target_agent)

        # Only process if relevant to this agent
        if self.agent_id not in (src_val, tgt_val):
            return

        is_local = src_val == tgt_val

        if is_local:
            logger.debug(
                "Local task update kept out of {} LLM queue: {}",
                self.agent_type,
                message.task_id,
            )
            return

        logger.debug(
            "Task update kept out of {} LLM queue: {} ({} -> {}, {})",
            self.agent_type,
            message.task_id,
            src_val,
            tgt_val,
            message.action,
        )

    async def _handle_report_message(self, message: Message) -> None:
        """Mark this turn 'reported' when this agent emits a ReportMessage.

        The turn-end guard checks _turn_reported to enforce that a
        Main-dispatched task ends with an explicit report.
        """
        if not isinstance(message, ReportMessage):
            return
        report_agent = normalize_agent_id(message.agent_type)
        if self.agent_id == "main" and self._task_board is not None:
            task = self._task_board.get_task(
                message.task_id,
                target_agent=report_agent,
                source_agent="main",
                active_only=False,
                session_id=self._current_session_id,
            ) or self._task_board.get_task(
                message.task_id,
                target_agent=report_agent,
                source_agent="main",
                active_only=False,
            )
            if task is not None and not bool(getattr(task, "blocking", True)):
                await self._message_queue.put(
                    UserMessage(
                        content=str(message.content or ""),
                        summary=str(message.summary or ""),
                        agent_type="main",
                        source=report_agent,
                    )
                )
                await self._publish_queue_update()
        if message.agent_type != self.agent_type:
            return
        self._turn_reported = True
        logger.debug(
            "{} turn reported (task={}, type={})",
            self.agent_type,
            message.task_id,
            message.report_type,
        )

    # ------------------------------------------------------------------ #
    #  Turn-end guards (report protocol)
    # ------------------------------------------------------------------ #

    _REPORT_GUARD_MAX_RETRIES = 2

    async def _run_guard_round(self, message: UserMessage, reminder: str) -> None:
        """Run one more LLM round after injecting a guard reminder.

        Reuses _build_messages + llm_provider.chat + _handle_tool_calls so the
        agent gets a real chance to call `report` (or resolve blocked tasks).
        """
        self._conversation_history.append(LLMMessage(role="user", content=reminder))
        try:
            messages = self._compact_working_memory(await self._build_messages())
            tool_defs = self.tools.get_definitions()
            response = await self._chat_with_retries(
                messages,
                tool_defs or None,
                getattr(message, "message_id", None),
                phase="guard",
                stream_idle_timeout_seconds=getattr(
                    self._current_message,
                    "provider_stream_idle_timeout_seconds",
                    None,
                ),
            )
        except Exception as e:
            logger.error("Guard re-prompt failed for {}: {}", self.agent_type, e)
            return

        if getattr(response, "tool_calls", None):

            @dataclass
            class _GuardResponse:
                content: str
                tool_calls: list
                thinking: str | None = None

            await self._handle_tool_calls(
                _GuardResponse(
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                    thinking=getattr(response, "thinking", None),
                ),
                getattr(message, "message_id", None),
            )
        elif response.content:
            self._buffered_main_response = response.content
            self._conversation_history.append(
                LLMMessage(role="assistant", content=response.content)
            )

    async def _enforce_report_guard(self, message: UserMessage) -> bool:
        """Sub-agents must call `respond` before ending a Main-dispatched turn.

        Returns True if the guard took over (caller must NOT finalize IDLE).
        Re-prompts up to _REPORT_GUARD_MAX_RETRIES times; if the agent still
        has not reported, marks the task BLOCKED so Main learns it needs action.
        """
        if self.agent_id == "main":
            return False
        if getattr(message, "source", None) != "main_agent":
            return False
        if not self._has_active_main_dispatched_task(message):
            return False
        if self._turn_reported:
            return False

        reminder = (
            "本轮需要调用 Respond 向 Main 回复。请调用 "
            "respond(task_id, type, summary, content)：summary 是必须填写的简短结果摘要，"
            "type='reply' 表示完成，"
            "'missing_data'/'quality' 表示卡住。"
        )
        while not self._turn_reported:
            self._report_retries += 1
            if self._report_retries > self._REPORT_GUARD_MAX_RETRIES:
                # Exhausted — mark the active Main-dispatched task BLOCKED.
                await self._mark_active_task_blocked()
                await self._set_status(AgentStatus.IDLE)
                self._report_retries = 0
                return True
            await self.bus.publish(
                SystemNotice(
                    agent_type=self.agent_type,
                    content="本轮需要调用 Respond 向 Main 回复",
                )
            )
            await self._run_guard_round(message, reminder)

        # The agent reported during a re-prompt round — finalize normally.
        await self._flush_manifest_if_needed()
        await self._set_status(AgentStatus.IDLE)
        return True

    def _has_active_main_dispatched_task(self, message: UserMessage) -> bool:
        """Return whether this Main-origin turn still has an active task to report."""
        if self._task_board is None:
            return True

        task_id = self._task_id_from_message(message)
        session_id = self._current_session_id
        if task_id:
            scoped = self._task_board.get_task(
                task_id,
                target_agent=self.agent_type,
                source_agent="main",
                active_only=True,
                session_id=session_id,
            )
            if scoped is not None:
                return True
            unscoped = self._task_board.get_task(
                task_id,
                target_agent=self.agent_type,
                source_agent="main",
                active_only=True,
            )
            return unscoped is not None

        todos = self._task_board.get_todolist(self.agent_type, session_id=session_id)
        if not todos:
            todos = self._task_board.get_todolist(self.agent_type)
        return any(
            t.source_agent == "main"
            and t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
            for t in todos
        )

    @staticmethod
    def _task_id_from_message(message: UserMessage) -> str | None:
        message_id = str(getattr(message, "message_id", "") or "").strip()
        if message_id.startswith("blocking:"):
            task_id = message_id.split(":", 1)[1].strip()
            return task_id or None
        return None

    async def _mark_active_task_blocked(self) -> None:
        """Mark the active Main->self task BLOCKED (auto-block fallback)."""
        if self._task_board is None:
            return
        todos = self._task_board.get_todolist(self.agent_type, session_id=self._current_session_id)
        if not todos:
            todos = self._task_board.get_todolist(self.agent_type)
        task = next((t for t in todos if t.source_agent == "main"), None)
        if task is None:
            return
        try:
            self._task_board.block_task(
                task.task_id, target_agent=self.agent_type, session_id=task.session_id
            )
        except ValueError:
            return
        await self.bus.publish(
            SystemNotice(
                agent_type=self.agent_type,
                content=f"多次未调用 Respond，任务 {task.task_id} 已标记为 blocked，交回 Main 处理。",
            )
        )

    async def _enforce_main_blocked_guard(self, message: UserMessage) -> bool:
        """Main may not go IDLE while it has BLOCKED tasks.

        Returns True if the guard took over. Re-prompts up to
        _REPORT_GUARD_MAX_RETRIES times; after that, allows IDLE and surfaces
        the unresolved blocks to the user via a SystemNotice.
        """
        if self.agent_id != "main" or self._task_board is None:
            return False

        reminder = (
            "还有被阻塞的报告任务未处理。请检查工作流返回的 decision_id 和缺资项，"
            "向用户说明可选决策；获得选择后用 resume_reporting_workflow 恢复同一 run。"
        )
        while True:
            blocked = self._task_board.get_blocked_waitlist(
                "main", session_id=self._current_session_id
            )
            if not blocked:
                self._main_block_retries = 0
                return False
            self._main_block_retries += 1
            names = ", ".join(f"{t.target_agent}:{t.brief}" for t in blocked)
            if self._main_block_retries > self._REPORT_GUARD_MAX_RETRIES:
                await self.bus.publish(
                    SystemNotice(
                        agent_type="main",
                        content=f"Main 多次未解决被阻塞任务，暂停以便用户介入：{names}",
                    )
                )
                self._main_block_retries = 0
                return False  # allow IDLE so the user can act
            await self.bus.publish(
                SystemNotice(
                    agent_type="main",
                    content=f"Main 还有被阻塞的任务：{names}，"
                    f"请先解决 ({self._main_block_retries}/{self._REPORT_GUARD_MAX_RETRIES})。",
                )
            )
            await self._run_guard_round(message, reminder)

    def _queue_message_summary(self, message: UserMessage) -> str:
        summary = str(getattr(message, "summary", "") or "").strip()
        if summary:
            if message.source == "main_agent":
                return f"From Main: {summary}"
            if message.source and message.source != "user":
                sender = get_agent_badge(message.source)
                return f"From {sender}: {summary}"
            return summary
        content = str(message.content or "").strip()
        first_line = content.splitlines()[0].strip() if content else ""
        if message.source == "main_agent":
            return f"From Main: {first_line}" if first_line else "From Main"
        if message.source == "system":
            return first_line or "System update"
        if message.source and message.source != "user":
            sender = get_agent_badge(message.source)
            return f"From {sender}: {first_line}" if first_line else f"From {sender}"
        return first_line or "Queued message"

    def _llm_content_for_user_message(self, message: UserMessage) -> str:
        content = str(message.content or "").strip()
        summary = str(getattr(message, "summary", "") or "").strip()
        if message.source == "main_agent" and summary:
            if content:
                return f"[Task Summary]\n{summary}\n\n[Task Detail]\n{content}"
            return f"[Task Summary]\n{summary}"
        return content

    def _latest_persisted_resumable_report_run_id(self) -> str | None:
        """Resolve the latest real terminal run mentioned in this conversation."""

        for prior in reversed(self._conversation_history):
            if prior.role != "user":
                continue
            try:
                payload = json.loads(str(prior.content or ""))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            run_id = str(payload.get("run_id", "") or "")
            status = str(payload.get("status", "") or "")
            if (
                not run_id.startswith("report-")
                or status not in _RESUMABLE_REPORT_STATUSES
                or Path(run_id).name != run_id
            ):
                continue
            run_root = self.workspace / "Work" / "runs" / run_id
            has_request = (run_root / "request.json").is_file() or (
                run_root / "revision-request.json"
            ).is_file()
            if has_request and (run_root / "workflow-state.json").is_file():
                return run_id
        return None

    async def _resume_simple_report_continuation(
        self, message: UserMessage
    ) -> bool:
        """Route a plain continuation directly to the persisted same-run resume tool."""

        if self.agent_id != "main":
            return False
        explicit_run_id = _explicit_report_continuation_run_id(message)
        simple_continuation = _is_simple_report_continuation(message)
        if not simple_continuation and explicit_run_id is None:
            return False
        run_id = explicit_run_id or self._latest_persisted_resumable_report_run_id()
        if run_id is None:
            await self._publish_resume_rejection(
                message,
                "没有从当前消息或最近的真实工作流终态中找到可恢复的 run_id；"
                "本轮没有新建报告运行。",
            )
            return True
        run_root = self.workspace / "Work" / "runs" / run_id
        has_request = (run_root / "request.json").is_file() or (
            run_root / "revision-request.json"
        ).is_file()
        if not has_request or not (run_root / "workflow-state.json").is_file():
            await self._publish_resume_rejection(
                message,
                f"报告任务 {run_id} 缺少持久化 request 或 workflow-state，"
                "不能从断点恢复；本轮没有新建报告运行。",
            )
            return True
        tool = self.tools.get("resume_reporting_workflow")
        if tool is None:
            await self._publish_resume_rejection(
                message,
                f"当前运行时没有提供 resume_reporting_workflow，无法恢复 {run_id}；"
                "本轮没有新建报告运行。",
            )
            return True

        arguments = {"run_id": run_id}
        logger.info("Direct same-run resume route selected: {}", run_id)
        await self._set_status(AgentStatus.RUNNING_TOOL)
        await self.bus.publish(
            ToolCallMessage(
                agent_type=self.agent_type,
                tool_name="resume_reporting_workflow",
                arguments=arguments,
            )
        )
        try:
            result = await tool(**arguments)
            outcome = normalize_tool_outcome(result, "resume_reporting_workflow")
            await self.bus.publish(
                ToolResultMsg(
                    agent_type=self.agent_type,
                    tool_name="resume_reporting_workflow",
                    result=result,
                    error=outcome.error if outcome.status != "ok" else None,
                )
            )
            self._terminal_outcome = outcome
            self._terminal_tool_name = "resume_reporting_workflow"
            response = canonical_terminal_message(outcome)
        except Exception as exc:
            logger.error("Deterministic report resume failed for {}: {}", run_id, exc)
            response = f"未能恢复报告任务 {run_id}：{exc}"
            await self.bus.publish(
                ToolResultMsg(
                    agent_type=self.agent_type,
                    tool_name="resume_reporting_workflow",
                    result=None,
                    error=str(exc),
                )
            )

        self._conversation_history.append(LLMMessage(role="assistant", content=response))
        await self.bus.publish(
            AgentResponse(
                agent_type=self.agent_type,
                content=response,
                message_id=message.message_id,
                streaming=False,
                **self._active_workflow_correlation(),
            )
        )
        await self._flush_manifest_if_needed()
        await self._set_status(AgentStatus.IDLE)
        return True

    async def _publish_resume_rejection(
        self, message: UserMessage, response: str
    ) -> None:
        """End an invalid resume instruction without falling back to new-run planning."""

        self._conversation_history.append(LLMMessage(role="assistant", content=response))
        await self.bus.publish(
            AgentResponse(
                agent_type=self.agent_type,
                content=response,
                message_id=message.message_id,
                streaming=False,
                **self._active_workflow_correlation(),
            )
        )
        await self._flush_manifest_if_needed()
        await self._set_status(AgentStatus.IDLE)

    def _must_buffer_main_report_route(self) -> bool:
        """Keep unverified Main prose out of the UI until a workflow receipt exists."""

        return self.agent_id == "main" and (
            _requires_reporting_workflow_route(self._current_message)
            or (
                (_report_workflow_terminal_payload(self._current_message) or {}).get(
                    "status"
                )
                == "failed"
            )
        )

    @staticmethod
    def _valid_failed_report_explanation(
        payload: dict[str, Any], content: str
    ) -> bool:
        """Require a grounded explanation and forbid unsupported operation claims."""

        text = str(content or "").strip()
        run_id = str(payload.get("run_id", "") or "")
        if not text or run_id not in text:
            return False
        if not re.search(r"失败|未通过|错误|异常|阻塞|未完成", text):
            return False
        if not re.search(r"原因|阶段|检查|校验|意味着|因此|未交付|下一步", text):
            return False
        if _UNVERIFIED_REPORT_OPERATION_CLAIM.search(text):
            return False
        mentioned_run_ids = set(re.findall(r"\breport-[A-Za-z0-9_-]+\b", text))
        return not mentioned_run_ids.difference({run_id})

    async def _enforce_failed_report_explanation(self, message: UserMessage) -> bool:
        """Let Main explain a failure while denying navigation or workflow actions."""

        payload = _report_workflow_terminal_payload(message)
        if self.agent_id != "main" or payload is None or payload.get("status") != "failed":
            return False

        for attempt in range(0, self._REPORT_GUARD_MAX_RETRIES + 1):
            candidate = self._buffered_main_response
            if self._valid_failed_report_explanation(payload, candidate):
                await self.bus.publish(
                    AgentResponse(
                        agent_type=self.agent_type,
                        content=candidate,
                        message_id=message.message_id,
                        streaming=False,
                        **self._active_workflow_correlation(),
                    )
                )
                await self._flush_manifest_if_needed()
                await self._set_status(AgentStatus.IDLE)
                return True
            if attempt >= self._REPORT_GUARD_MAX_RETRIES:
                break
            self._buffered_main_response = ""
            await self.bus.publish(
                SystemNotice(
                    agent_type="main",
                    content=(
                        "失败说明包含未经回执的运行声明或未绑定真实 run_id，"
                        f"正在要求 Main 重写（{attempt + 1}/"
                        f"{self._REPORT_GUARD_MAX_RETRIES}）。"
                    ),
                )
            )
            await self._run_guard_round(
                message,
                (
                    "只根据当前 report-workflow 终态中的 run_id、status 和 error，"
                    "向用户解释失败发生了什么、意味着什么以及下一步可选择恢复原 run。"
                    "本回合不得调用任何工具，不得声称已启动、正在运行或已经恢复，"
                    "不得生成其他 run_id。回复必须明确写出真实 run_id。"
                ),
            )

        fallback = _canonical_failed_report_response(message)
        if fallback is None:
            return False
        self._conversation_history.append(LLMMessage(role="assistant", content=fallback))
        await self.bus.publish(
            AgentResponse(
                agent_type=self.agent_type,
                content=fallback,
                message_id=message.message_id,
                streaming=False,
                **self._active_workflow_correlation(),
            )
        )
        await self._flush_manifest_if_needed()
        await self._set_status(AgentStatus.IDLE)
        return True

    async def _enforce_main_reporting_route(self, message: UserMessage) -> bool:
        """Require a real terminal workflow-tool receipt for direct report actions."""

        if self.agent_id != "main" or not _requires_reporting_workflow_route(message):
            return False
        if (
            self._terminal_outcome is not None
            and self._terminal_tool_name in _REPORTING_ENTRY_TOOLS
        ):
            return False

        reminder = (
            "当前用户消息要求执行报告操作，但本轮尚无 run_reporting_workflow、"
            "resume_reporting_workflow 或 revise_reporting_workflow 的真实工具回执。"
            "不要声称任务已启动，不要编造 run_id；现在调用与该请求匹配的工作流工具。"
        )
        for attempt in range(1, self._REPORT_GUARD_MAX_RETRIES + 1):
            await self.bus.publish(
                SystemNotice(
                    agent_type="main",
                    content=(
                        "报告操作尚未获得真实工作流回执，正在要求 Main 完成路由"
                        f"（{attempt}/{self._REPORT_GUARD_MAX_RETRIES}）。"
                    ),
                )
            )
            await self._run_guard_round(message, reminder)
            if (
                self._terminal_outcome is not None
                and self._terminal_tool_name in _REPORTING_ENTRY_TOOLS
            ):
                await self._flush_manifest_if_needed()
                await self._set_status(AgentStatus.IDLE)
                return True
            if self._terminal_outcome is not None:
                break

        response = (
            "本轮没有获得报告工作流工具的成功回执，因此没有启动、恢复或修订任何报告，"
            "也没有生成有效 run_id。"
        )
        self._conversation_history.append(LLMMessage(role="assistant", content=response))
        await self.bus.publish(
            AgentResponse(
                agent_type=self.agent_type,
                content=response,
                message_id=message.message_id,
                streaming=False,
                **self._active_workflow_correlation(),
            )
        )
        await self._flush_manifest_if_needed()
        await self._set_status(AgentStatus.IDLE)
        return True

    async def _publish_queue_update(self) -> None:
        queued = list(self._message_queue._queue)
        summaries = [
            self._queue_message_summary(msg)
            for msg in queued
            if str(getattr(msg, "source", "user") or "user") == "user"
        ]
        await self.bus.publish(
            QueueUpdateMessage(
                agent_type=self.agent_type,
                queued_messages=summaries,
            )
        )

    def _active_workflow_correlation(self) -> dict[str, str]:
        """Copy the active typed-task identity onto every response/error event."""

        message = self._current_message
        if message is None:
            return {}
        return {
            "workflow_id": str(getattr(message, "workflow_id", "") or ""),
            "run_id": str(getattr(message, "run_id", "") or ""),
            "task_id": str(getattr(message, "task_id", "") or ""),
            "task_attempt_id": str(
                getattr(message, "task_attempt_id", "") or ""
            ),
            "session_id": str(getattr(message, "session_id", "") or ""),
        }

    async def _process_message(self, message: UserMessage) -> None:
        """Process a user message.

        Creates a per-agent checkpoint before processing so the agent can
        roll back to the pre-message file state.

        Args:
            message: User message to process.
        """
        # Keep direct test/debug invocations under the same routing policy as
        # messages dequeued by _process_loop.
        self._current_message = message
        now = datetime.now(tz=message.timestamp.tzinfo)
        self._usage_queue_wait_ms = max(
            0,
            int((now - message.timestamp).total_seconds() * 1000),
        )
        self._usage_context_build_ms = 0
        self._usage_tool_time_ms = 0
        await self._set_status(AgentStatus.THINKING)

        # Reset per-turn state: this message's turn has not yet been reported.
        self._turn_reported = False
        self._terminal_outcome = None
        self._terminal_tool_name = None
        self._buffered_main_response = ""
        self._report_retries = 0
        self._main_block_retries = 0
        self._progress_monitor = _ProgressMonitor()
        # Reset cancel event for this message
        self._cancel_event.clear()

        # Create a pre-message checkpoint for this agent
        cp_id = None
        if self._loop_manager is not None:
            try:
                source = message.source if hasattr(message, "source") else "user"
                agent_str = self._get_agent_type_str()
                # Save conversation history to checkpoint
                history_dicts = [
                    msg.model_dump()
                    if hasattr(msg, "model_dump")
                    else {"role": msg.role, "content": msg.content}
                    for msg in self._conversation_history
                ]
                cp_id = await self._loop_manager.create_checkpoint(
                    agent_type=agent_str,
                    description=f"pre:{source}",
                    source="pre_message",
                    conversation_history=history_dicts,
                    message_id=message.message_id,
                )
                logger.debug("{} checkpoint created: {}", agent_str, cp_id)
            except Exception as e:
                logger.warning("Failed to create checkpoint for {}: {}", self.agent_type, e)

        try:
            # In debug mode, wrap message with context
            content = self._llm_content_for_user_message(message)
            if self._debug_mode:
                content = (
                    "[调试模式] 此 Agent 处于独立调试模式，不与其他 Agent 通信。\n"
                    "你可以直接测试此 Agent 的工具和输出。\n\n" + content
                )

            # Add user message to conversation history
            self._conversation_history.append(LLMMessage(role="user", content=content))

            if await self._resume_simple_report_continuation(message):
                return

            # Build messages — system prompt always first, conversation history follows
            context_started = time.perf_counter()
            messages = await self._build_messages()

            # Auto-compact: trim if exceeds context budget
            messages = self._compact_working_memory(messages)

            # Get tool definitions
            tool_definitions = self.tools.get_definitions()
            self._usage_context_build_ms = int(
                (time.perf_counter() - context_started) * 1000
            )

            # Call LLM with streaming (default)
            start_time = time.time()

            accumulated_content = ""
            accumulated_tool_calls = []
            accumulated_thinking = None
            last_error = None
            # Whether the assistant turn has already been appended to history
            # (set when the stream completes normally). Guards against a
            # double-append if a cancel arrives in the same instant as `done`.
            stream_committed = False
            response = _LoopLLMResponse(content="", tool_calls=[])

            try:
                response = await self._chat_with_retries(
                    messages,
                    tool_definitions if tool_definitions else None,
                    message.message_id,
                    phase="initial",
                    stream_idle_timeout_seconds=(
                        message.provider_stream_idle_timeout_seconds
                    ),
                )
                accumulated_content = response.content
                self._buffered_main_response = accumulated_content
                accumulated_tool_calls = response.tool_calls
                accumulated_thinking = response.thinking
                if (
                    accumulated_content
                    and not accumulated_tool_calls
                    and response.stop_reason not in {"max_tokens", "length"}
                ):
                    self._conversation_history.append(
                        LLMMessage(role="assistant", content=accumulated_content)
                    )
                    stream_committed = True
            except Exception as e:
                last_error = str(e)
                raise

            finally:
                # Calculate duration and publish debug info
                duration_ms = int((time.time() - start_time) * 1000)

                usage_record = self._last_usage_record or {
                    "input_tokens": 0,
                    "output_tokens": 0,
                }

                await self.bus.publish(
                    ApiDebugMessage(
                        model=self.llm_provider.model or "unknown",
                        tokens_in=usage_record["input_tokens"],
                        tokens_out=usage_record["output_tokens"],
                        duration_ms=duration_ms,
                        status="cancelled"
                        if self._cancel_event.is_set()
                        else ("success" if last_error is None else "error"),
                        error=last_error,
                    )
                )

            # Handle cancellation — assemble the interrupt event into context,
            # then emit a UI notice. Skip tool calls.
            if self._cancel_event.is_set():
                # If the stream already completed normally (done fired) a cancel
                # that arrived in the same instant must not be treated as an
                # interrupt — the turn already finished.
                if not stream_committed:
                    # Assemble the interrupt event into the conversation so the
                    # NEXT turn can see that this response was cut off. Always
                    # append an assistant turn to preserve user/assistant
                    # alternation — even when no tokens were produced (otherwise
                    # the queued user message would follow the previous user
                    # message and break alternating-role APIs like Anthropic).
                    partial = (accumulated_content or "").rstrip()
                    if partial:
                        interrupt_content = f"{partial}\n\n[Interrupted by user]"
                    else:
                        interrupt_content = (
                            "[Interrupted by user — response stopped before any output.]"
                        )
                    self._conversation_history.append(
                        LLMMessage(role="assistant", content=interrupt_content)
                    )
                # UI: render a muted italic "Interrupted" notice instead of a
                # fake "[已取消]" assistant bubble.
                await self.bus.publish(
                    SystemNotice(
                        agent_type=self.agent_type,
                        content="Interrupted",
                        kind="interrupt",
                    )
                )
                await self._set_status(AgentStatus.IDLE)
                return

            max_tokens_continuation_required = (
                response.stop_reason in {"max_tokens", "length"}
                and str(getattr(message, "source", "") or "") == "workflow"
                and not accumulated_tool_calls
                and not self._turn_reported
            )
            if max_tokens_continuation_required:
                partial = (accumulated_content or "").rstrip()
                continuation_marker = (
                    "[Provider output reached the per-request max_tokens limit "
                    "before a typed tool submission. The task is not complete.]"
                )
                self._conversation_history.append(
                    LLMMessage(
                        role="assistant",
                        content=(
                            f"{partial}\n\n{continuation_marker}"
                            if partial
                            else continuation_marker
                        ),
                    )
                )
                await self.bus.publish(
                    AgentResponse(
                        agent_type=self.agent_type,
                        content=AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
                        message_id=message.message_id,
                        streaming=False,
                        internal=True,
                        **self._active_workflow_correlation(),
                    )
                )
                await self._flush_manifest_if_needed()
                await self._set_status(AgentStatus.IDLE)
                return

            # Handle tool calls if present
            if accumulated_tool_calls:

                @dataclass
                class StreamResponse:
                    content: str
                    tool_calls: list
                    thinking: str | None = None

                response = StreamResponse(
                    content=accumulated_content,
                    tool_calls=accumulated_tool_calls,
                    thinking=accumulated_thinking,
                )
                await self._handle_tool_calls(response, message.message_id)
                if self._cancel_event.is_set():
                    return

            if await self._enforce_failed_report_explanation(message):
                return

            if await self._enforce_main_reporting_route(message):
                return

            if self._terminal_outcome is not None:
                await self._flush_manifest_if_needed()
                await self._set_status(AgentStatus.IDLE)
                return

            # Send final completion signal.
            # For tool-call paths, _handle_tool_calls already published the
            # final content — this is just a completion marker.
            # For no-tool paths, include the accumulated content so that
            # SendToAgentTool (and any other bus listener) receives the
            # full response text on the non-streaming message.
            final_content = "" if accumulated_tool_calls else accumulated_content
            await self.bus.publish(
                AgentResponse(
                    agent_type=self.agent_type,
                    content=final_content,
                    message_id=message.message_id,
                    streaming=False,
                    **self._active_workflow_correlation(),
                )
            )

            await self._flush_manifest_if_needed()

            # Turn-end guards (report protocol). Each guard may re-prompt the
            # agent instead of going IDLE; if a guard takes over, it owns the
            # final status transition and we return without double-finalizing.
            if await self._enforce_report_guard(message):
                return
            if await self._enforce_main_blocked_guard(message):
                return

            await self._set_status(AgentStatus.IDLE)

        except Exception as e:
            logger.error("Error processing message in {}: {}", self.agent_type, str(e))
            self._consecutive_errors += 1

            if isinstance(e, _ProviderRequestError):
                display_error = str(e)
                details = runtime_error_details(e.original)
                details.update(
                    {
                        "attempts": e.attempts,
                        "automatic_retries": max(0, e.attempts - 1),
                        "partial_output": e.partial_output,
                        "ambiguous": e.ambiguous,
                        "attempt_disposition": e.attempt_disposition,
                    }
                )
            else:
                policy = classify_runtime_error(e)
                display_error = f"{policy.title}：{e}"
                details = runtime_error_details(e)

            if self._consecutive_errors >= 3:
                logger.warning(
                    "{}: 3 consecutive errors — skipping current message", self.agent_type
                )

            await self._flush_manifest_if_needed()
            await self._set_status(AgentStatus.ERROR)
            await self.bus.publish(
                Error(
                    source=str(self.agent_type),
                    message=display_error,
                    details=details,
                    **self._active_workflow_correlation(),
                )
            )

        else:
            self._consecutive_errors = 0

    async def _flush_manifest_if_needed(self) -> None:
        """Wrap up manifest at end of loop: prompt agent to update free-text notes.

        Only fires when file tools were used during this turn.  Injects a
        lightweight system instruction so the agent can decide whether the
        notes section needs updating — no extra LLM round required; the
        hint is prepended to the next turn's system prompt.
        """
        if self.agent_id == "main" or self._manifest_manager is None:
            self._manifest_dirty = False
            return

        if not self._manifest_dirty:
            return

        self._manifest_dirty = False

        # Inject a brief wrap-up prompt so the agent reviews the manifest
        # notes *at the start of the next turn*.  That avoids an extra LLM
        # call while still giving the agent a chance to update the free-text
        # section when it next processes a message.
        try:
            manifest = await self._manifest_manager.load(self._get_agent_type_str())
            files = manifest.get("files", [])
            notes = manifest.get("notes", "")
            # Only poke the agent when there are un-described files or the
            # notes area is empty after file changes.
            undescribed = [f for f in files if not f.get("description", "").strip()]
            if undescribed or not notes.strip():
                hint = (
                    "\n\n[Manifest 收尾提示]\n本轮的创建/修改/删除文件已自动更新到你的 manifest。"
                )
                if undescribed:
                    hint += "以下文件尚无描述，请在方便时通过 manifest 工具补全：\n" + "\n".join(
                        f"  - {f['path']}" for f in undescribed
                    )
                if not notes.strip():
                    hint += (
                        "\n自由文本区（notes）当前为空，请在方便时补充文件之间的关系、"
                        "依赖或协作说明。"
                    )
                else:
                    hint += "\n自由文本区（notes）可能需要补充。请查看并根据需要更新。"
                self._conversation_history.append(LLMMessage(role="user", content=hint))
        except Exception as e:
            logger.warning("Failed to flush manifest for {}: {}", self.agent_type, e)

    async def _chat_followup(
        self,
        messages: list[LLMMessage],
        tool_definitions: list[dict] | None,
        user_message_id: str | None,
        stream_idle_timeout_seconds: float | None = None,
    ) -> _LoopLLMResponse:
        """Run a post-tool LLM round, streaming UI deltas when supported."""
        accumulated_content = ""
        accumulated_tool_calls = []
        accumulated_thinking = ""
        final_usage = None
        final_request_metrics = None
        stop_reason = None
        provider_started = time.perf_counter()
        ttft_ms: int | None = None
        try:
            try:
                stream_kwargs: dict[str, Any] = {}
                if stream_idle_timeout_seconds is not None:
                    stream_kwargs["stream_idle_timeout_seconds"] = (
                        stream_idle_timeout_seconds
                    )
                stream = self.llm_provider.chat_stream(
                    messages=messages,
                    tools=tool_definitions if tool_definitions else None,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    **stream_kwargs,
                )
                if not hasattr(stream, "__aiter__"):
                    await stream
                    raise NotImplementedError
                async for chunk in stream:
                    if ttft_ms is None and (
                        chunk.delta
                        or chunk.tool_calls
                        or chunk.thinking
                        or chunk.done
                    ):
                        ttft_ms = int(
                            (time.perf_counter() - provider_started) * 1000
                        )
                    if chunk.delta:
                        accumulated_content += chunk.delta
                        if not self._must_buffer_main_report_route():
                            await self.bus.publish(
                                AgentResponse(
                                    agent_type=self.agent_type,
                                    content=chunk.delta,
                                    message_id=user_message_id,
                                    streaming=True,
                                    **self._active_workflow_correlation(),
                                )
                            )

                    if chunk.tool_calls:
                        accumulated_tool_calls = chunk.tool_calls

                    if chunk.thinking:
                        if chunk.done:
                            # Some streaming providers repeat the complete thinking
                            # snapshot in the terminal chunk after already emitting
                            # every thinking delta.  Treat it as a snapshot, not a
                            # second delta, while still accepting providers that only
                            # expose thinking at completion.
                            if not accumulated_thinking:
                                accumulated_thinking = chunk.thinking
                            elif chunk.thinking.startswith(accumulated_thinking):
                                accumulated_thinking = chunk.thinking
                            elif not accumulated_thinking.endswith(chunk.thinking):
                                accumulated_thinking += chunk.thinking
                        else:
                            accumulated_thinking += chunk.thinking
                            await self.bus.publish(
                                AgentResponse(
                                    agent_type=self.agent_type,
                                    content="",
                                    message_id=user_message_id,
                                    streaming=True,
                                    thinking=chunk.thinking,
                                    **self._active_workflow_correlation(),
                                )
                            )

                    if chunk.usage:
                        final_usage = chunk.usage
                    if chunk.request_metrics:
                        final_request_metrics = chunk.request_metrics
                    if chunk.stop_reason:
                        stop_reason = chunk.stop_reason

                    if chunk.done or self._cancel_event.is_set():
                        break

                return _LoopLLMResponse(
                    content=accumulated_content,
                    tool_calls=accumulated_tool_calls,
                    thinking=accumulated_thinking or None,
                    usage=final_usage,
                    stop_reason=stop_reason,
                    streamed=True,
                    request_metrics=final_request_metrics,
                    ttft_ms=ttft_ms,
                    provider_active_ms=int(
                        (time.perf_counter() - provider_started) * 1000
                    ),
                )
            except NotImplementedError:
                response = await self.llm_provider.chat(
                    messages=messages,
                    tools=tool_definitions,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                )
                return _LoopLLMResponse(
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                    thinking=getattr(response, "thinking", None),
                    usage=getattr(response, "usage", None),
                    stop_reason=getattr(response, "stop_reason", None),
                    streamed=False,
                    request_metrics=getattr(response, "request_metrics", None),
                    ttft_ms=int(
                        (time.perf_counter() - provider_started) * 1000
                    ),
                    provider_active_ms=int(
                        (time.perf_counter() - provider_started) * 1000
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            partial_output = bool(
                accumulated_content or accumulated_tool_calls or accumulated_thinking
            )
            raise _ProviderAttemptError(exc, partial_output=partial_output) from exc

    async def _wait_before_retry(self, delay_seconds: float) -> bool:
        """Wait for a retry delay; return True when the user interrupts it."""

        if delay_seconds <= 0:
            return self._cancel_event.is_set()
        try:
            await asyncio.wait_for(self._cancel_event.wait(), timeout=delay_seconds)
            return True
        except TimeoutError:
            return False

    async def _chat_with_retries(
        self,
        messages: list[LLMMessage],
        tool_definitions: list[dict] | None,
        user_message_id: str | None,
        *,
        phase: str = "provider",
        stream_idle_timeout_seconds: float | None = None,
    ) -> _LoopLLMResponse:
        """Run one provider round with bounded, cancel-aware automatic retries."""

        provider_messages = _sanitize_provider_messages(messages)
        provider_tool_definitions = _sanitize_provider_visible_value(
            tool_definitions
        )
        attempts = 0
        while True:
            attempts += 1
            attempt_started = time.monotonic()
            if self.before_provider_attempt is not None:
                await self.before_provider_attempt()
            if self.provider_attempt_observer is not None:
                await self.provider_attempt_observer(
                    provider_messages,
                    provider_tool_definitions,
                    phase,
                    attempts,
                )
            try:
                response = await self._chat_followup(
                    provider_messages,
                    provider_tool_definitions,
                    user_message_id,
                    stream_idle_timeout_seconds,
                )
                self._last_usage_record = self._record_token_usage(
                    provider_messages,
                    response,
                    phase=phase,
                    status="success",
                    error=None,
                    attempt=attempts,
                    attempt_disposition="completed",
                    retry_decision="completed",
                    tool_definitions=provider_tool_definitions,
                    duration_ms=int((time.monotonic() - attempt_started) * 1000),
                )
                await self._notify_provider_attempt_record(self._last_usage_record)
                return response
            except _ProviderAttemptError as failure:
                retry_number = attempts
                policy = classify_runtime_error(failure.original, retry_number=retry_number)
                attempt_disposition = failure.attempt_disposition
                ambiguous = (
                    failure.partial_output
                    or attempt_disposition
                    == ProviderRequestDisposition.ACCEPTED_OR_UNKNOWN
                )
                within_retry_boundary = attempts <= _MAX_PROVIDER_RETRIES
                safe_to_repeat = attempt_disposition in {
                    ProviderRequestDisposition.NOT_SENT,
                    ProviderRequestDisposition.DEFINITELY_REJECTED,
                }
                can_retry = (
                    policy.retryable
                    and safe_to_repeat
                    and not failure.partial_output
                    and within_retry_boundary
                    and not self._cancel_event.is_set()
                )
                if can_retry:
                    retry_decision = "automatic_retry"
                elif failure.partial_output:
                    retry_decision = "stop_partial_output"
                elif ambiguous:
                    retry_decision = "stop_ambiguous"
                elif not policy.retryable:
                    retry_decision = "stop_non_retryable"
                elif not within_retry_boundary:
                    retry_decision = "stop_retry_limit"
                elif self._cancel_event.is_set():
                    retry_decision = "stop_cancelled"
                else:
                    retry_decision = "stop_not_proven_safe"
                self._last_usage_record = self._record_token_usage(
                    provider_messages,
                    _LoopLLMResponse(content="", tool_calls=[]),
                    phase=phase,
                    status="error",
                    error=str(failure.original),
                    attempt=attempts,
                    retry=can_retry,
                    attempt_disposition=attempt_disposition.value,
                    retry_decision=retry_decision,
                    error_class=type(failure.original).__name__,
                    tool_definitions=provider_tool_definitions,
                    duration_ms=int((time.monotonic() - attempt_started) * 1000),
                )
                await self._notify_provider_attempt_record(self._last_usage_record)
                if not can_retry:
                    raise _ProviderRequestError(
                        failure.original,
                        policy,
                        attempts=attempts,
                        partial_output=failure.partial_output,
                        ambiguous=ambiguous,
                        attempt_disposition=attempt_disposition,
                    ) from failure.original

                visible_agent = (
                    "main" if "--session-" in str(self.agent_type) else self.agent_type
                )
                await self.bus.publish(
                    SystemNotice(
                        agent_type=visible_agent,
                        content=(
                            f"{policy.title}：{failure.original}\n"
                            f"将在 {policy.delay_seconds:g} 秒后自动重试"
                            f"（{attempts}/{_MAX_PROVIDER_RETRIES}）。"
                        ),
                    )
                )
                if await self._wait_before_retry(policy.delay_seconds):
                    return _LoopLLMResponse(content="", tool_calls=[])

    async def _notify_provider_attempt_record(
        self,
        record: dict[str, Any],
    ) -> None:
        """Backfill optional reporting telemetry without failing paid work."""

        if self.provider_attempt_record_observer is None:
            return
        try:
            await self.provider_attempt_record_observer(record)
        except Exception as exc:
            logger.warning(
                "Provider attempt telemetry finalizer failed for {}: {}",
                self.agent_type,
                exc,
            )

    async def _handle_tool_calls(
        self,
        response,
        user_message_id: str | None,
    ) -> None:
        """Handle tool calls from LLM response.

        Stores structured messages (not formatted text) so each provider
        can convert them to the correct API format.
        """
        max_iterations = self.config.max_tool_iterations
        iteration = 0

        current_messages = await self._build_messages()

        while response.tool_calls and iteration < max_iterations:
            iteration += 1
            round_results: list[str] = []
            logger.debug(
                "Tool iteration {} for agent: {}, tool_calls: {}",
                iteration,
                self.agent_type,
                len(response.tool_calls),
            )

            # Add assistant message with tool calls as structured data
            assistant_tool_message = LLMMessage(
                role="assistant",
                content=response.content or "",
                tool_calls=[
                    LLMToolCall(
                        id=call.id,
                        name=call.name,
                        arguments=dict(call.arguments or {}),
                    )
                    for call in response.tool_calls
                ],
                thinking=response.thinking,
            )
            current_messages.append(assistant_tool_message)

            # Execute a bounded batch. Every skipped call still gets a compact
            # protocol-valid tool result so providers do not see orphan calls.
            batch_limit = self.config.max_tool_calls_per_round
            parallel_results: dict[int, Any] = {}
            bounded_calls = list(response.tool_calls[:batch_limit])
            pure_batch = len(bounded_calls) >= 2 and len(response.tool_calls) <= batch_limit
            if pure_batch:
                for candidate in bounded_calls:
                    candidate_tool = self.tools.get(candidate.name)
                    required = _tool_required_args(
                        self.tools,
                        candidate.name,
                        candidate_tool,
                    )
                    if (
                        candidate_tool is None
                        or candidate_tool.side_effect != "pure_read"
                        or not candidate_tool.parallel_safe
                        or any(
                            name not in candidate.arguments
                            or candidate.arguments.get(name) is None
                            for name in required
                        )
                    ):
                        pure_batch = False
                        break
            if pure_batch:
                batch_started = time.perf_counter()

                async def run_pure_call(call: LLMToolCall) -> Any:
                    tool = self.tools.get(call.name)
                    if tool is None:
                        raise ValueError(f"Tool not found: {call.name}")
                    return await tool(**dict(call.arguments or {}))

                batch_task = asyncio.gather(
                    *(run_pure_call(call) for call in bounded_calls),
                    return_exceptions=True,
                )
                self._active_tool_task = batch_task
                try:
                    batch_values = await batch_task
                finally:
                    self._usage_tool_time_ms += int(
                        (time.perf_counter() - batch_started) * 1000
                    )
                    if self._active_tool_task is batch_task:
                        self._active_tool_task = None
                parallel_results = dict(enumerate(batch_values))
            for tool_index, tool_call in enumerate(response.tool_calls):
                if tool_index >= batch_limit:
                    result_str = (
                        "Tool call skipped: this round exceeded the maximum batch of "
                        f"{batch_limit}. Use the results already returned, then request only "
                        "the next necessary tool calls."
                    )
                    await self.bus.publish(
                        ToolResultMsg(
                            agent_type=self.agent_type,
                            tool_name=tool_call.name,
                            result=None,
                            error=result_str,
                        )
                    )
                    provider_class_name = self.llm_provider.__class__.__name__
                    tool_result_role = (
                        "user" if provider_class_name == "AnthropicProvider" else "tool"
                    )
                    current_messages.append(
                        LLMMessage(
                            role=tool_result_role,
                            content=result_str,
                            tool_call_id=tool_call.id,
                            is_tool_result=True,
                        )
                    )
                    round_results.append(result_str)
                    continue
                await self._set_status(AgentStatus.RUNNING_TOOL)

                historical_marker_part_ids = _unresolved_persisted_result_part_ids(
                    tool_call
                )
                execution_tool_call = _rehydrate_persisted_result_part_call(
                    tool_call,
                    self._persisted_result_part_contents,
                    self.workspace,
                    str(self.usage_run_id) if self.usage_run_id else None,
                    str(self.usage_task_id) if self.usage_task_id else None,
                )
                unresolved_part_ids = _unresolved_persisted_result_part_ids(
                    execution_tool_call
                )
                resolved_historical_marker = bool(historical_marker_part_ids) and not (
                    unresolved_part_ids
                )
                visible_tool_call = (
                    _redact_unresolved_persisted_result_part_call(
                        tool_call
                    )
                    if historical_marker_part_ids
                    else tool_call
                )
                # A retired marker is never a Provider-visible success example,
                # even when its same-run digest can be verified from disk.
                assistant_tool_message.tool_calls[tool_index] = visible_tool_call
                await self.bus.publish(
                    ToolCallMessage(
                        agent_type=self.agent_type,
                        tool_name=tool_call.name,
                        arguments=visible_tool_call.arguments,
                    )
                )

                try:
                    if unresolved_part_ids:
                        raise _PersistedResultPartCorrection(
                            execution_tool_call,
                            unresolved_part_ids,
                        )
                    tool = self.tools.get(tool_call.name)
                    if tool is None:
                        raise ValueError(f"Tool not found: {tool_call.name}")

                    if resolved_historical_marker:
                        # Legacy checkpoints described an already-persisted part
                        # by marker.  Replaying the historical write can overwrite
                        # good prose and creates another paid correction loop.  A
                        # verified marker therefore closes as an idempotent read of
                        # durable state; the current schema/list tool determines the
                        # next action.
                        result = {
                            "status": "already_ready",
                            "accepted": True,
                            "persisted": True,
                            "ready_part_ids": historical_marker_part_ids,
                            "do_not_rewrite_part_ids": historical_marker_part_ids,
                            "next_action": "list_result_parts_then_submit",
                            "instruction": (
                                "The referenced same-run parts are already persisted. "
                                "Do not execute the historical write again. List current "
                                "parts once, write only missing or explicitly authorized "
                                "rewrite ids, then submit the typed result."
                            ),
                        }

                    elif (
                        tool_call.name == "cancel_reporting_workflow"
                        and not _is_explicit_report_cancel_request(self._current_message)
                    ):
                        raise PermissionError(
                            "cancel_reporting_workflow requires an explicit cancellation "
                            "instruction in the current end-user message; Main may not "
                            "cancel a report while replanning or handling workflow messages"
                        )

                    if (
                        self.agent_id == "main"
                        and tool_call.name == "resume_reporting_workflow"
                        and execution_tool_call.arguments.get("decision_id") is not None
                        and not _is_explicit_evidence_decision(
                            self._current_message,
                            str(execution_tool_call.arguments.get("action") or ""),
                        )
                    ):
                        raise PermissionError(
                            "Evidence-decision resume requires the current end-user "
                            "message to explicitly select the submitted action. Main may "
                            "explain supplement/draft/skip/stop options but may not choose "
                            "one from a report-workflow terminal or guard re-prompt."
                        )

                    if (
                        self.agent_id == "main"
                        and tool_call.name in _REPORT_ROUTE_FILE_TOOLS
                        and _requires_reporting_workflow_route(self._current_message)
                    ):
                        raise PermissionError(
                            "This direct report operation must be routed before project "
                            "content is read. Select the explicit operation and call "
                            "run_reporting_workflow once (or the matching resume/revise "
                            "tool for an existing run); do not traverse Inputs, Knowledge, "
                            "Templates, Work, or tool-result artifacts from Main."
                        )

                    terminal_payload = _report_workflow_terminal_payload(
                        self._current_message
                    )
                    if (
                        self.agent_id == "main"
                        and terminal_payload is not None
                        and terminal_payload.get("status") == "failed"
                    ):
                        raise PermissionError(
                            "A failed report-workflow terminal turn is explanation-only. "
                            "Do not call files, status, run, resume, revise, cancel, or any "
                            "other tool until the user sends a new instruction."
                        )

                    required_args = _tool_required_args(self.tools, tool_call.name, tool)

                    # Detect truncated tool calls: output hit max_tokens before arguments were complete
                    if not execution_tool_call.arguments and required_args:
                        usage = response.usage or {}
                        output_tokens = usage.get("output_tokens", 0)
                        required_hint = (
                            f" Required arguments: {', '.join(required_args)}."
                            if required_args
                            else ""
                        )
                        if output_tokens >= 8192:
                            raise ValueError(
                                f"Tool call to '{tool_call.name}' has no arguments — "
                                f"the response was truncated at {output_tokens} output tokens. "
                                f"Your output is too long. Break the file into smaller parts "
                                f"and write them one at a time using apply_patch, or use a shorter response."
                                f"{required_hint}"
                            )
                        else:
                            raise _ToolInputCorrection(
                                execution_tool_call,
                                required_argument_names=required_args,
                                missing_argument_names=required_args,
                            )

                    missing_required = [
                        name for name in required_args
                        if (
                            name not in execution_tool_call.arguments
                            or execution_tool_call.arguments.get(name) is None
                        )
                    ]
                    if missing_required:
                        raise _ToolInputCorrection(
                            execution_tool_call,
                            required_argument_names=required_args,
                            missing_argument_names=missing_required,
                        )

                    if resolved_historical_marker:
                        pass
                    elif tool_index in parallel_results:
                        result = parallel_results[tool_index]
                        if isinstance(result, BaseException):
                            raise result
                    else:
                        tool_started = time.perf_counter()
                        tool_task = asyncio.create_task(
                            tool(**execution_tool_call.arguments)
                        )
                        self._active_tool_task = tool_task
                        try:
                            result = await tool_task
                        finally:
                            self._usage_tool_time_ms += int(
                                (time.perf_counter() - tool_started) * 1000
                            )
                            if self._active_tool_task is tool_task:
                                self._active_tool_task = None

                    if (
                        tool_call.name == "apply_patch"
                        and isinstance(result, dict)
                        and str(result.get("error") or "").strip()
                    ):
                        raise RuntimeError(str(result.get("error") or "").strip())

                    if tool_call.name in ("apply_patch", "delete_file"):
                        self._manifest_dirty = True
                    if (
                        tool_call.name == "respond"
                        and isinstance(result, dict)
                        and result.get("status") == "ok"
                    ):
                        self._turn_reported = True

                    outcome = normalize_tool_outcome(result, tool_call.name)
                    if outcome.status == "ok" and assistant_tool_message.tool_calls:
                        if (
                            not resolved_historical_marker
                            and isinstance(result, dict)
                            and result.get("persisted") is True
                        ):
                            _remember_persisted_result_part_content(
                                execution_tool_call,
                                self._persisted_result_part_contents,
                            )
                        # Keep successful tool history protocol-valid. In particular,
                        # never delete required ``content`` arguments from a prior
                        # result-part write call: providers treat the transcript as
                        # an in-context example and can otherwise imitate the malformed
                        # call even when its paired tool result says it succeeded.
                        # The general working-memory compactor bounds cost by evicting
                        # complete older messages and persists their exact transcript.
                        assistant_tool_message.tool_calls[tool_index] = (
                            visible_tool_call
                            if resolved_historical_marker
                            else execution_tool_call
                        )
                    await self.bus.publish(
                        ToolResultMsg(
                            agent_type=self.agent_type,
                            tool_name=tool_call.name,
                            result=result,
                            error=outcome.error if outcome.status != "ok" else None,
                        )
                    )

                    result_str = self._format_tool_result(
                        result,
                        tool_name=tool_call.name,
                        tool_call_id=tool_call.id,
                    )

                    if outcome.terminal:
                        self._terminal_outcome = outcome
                        self._terminal_tool_name = tool_call.name

                except _PersistedResultPartCorrection as correction:
                    assistant_tool_message.tool_calls[tool_index] = (
                        _redact_unresolved_persisted_result_part_call(
                            correction.tool_call
                        )
                    )
                    logger.info(
                        "Result-part history placeholder rejected for {} on agent {}; "
                        "requested provider correction for parts={}",
                        tool_call.name,
                        self.agent_type,
                        correction.payload["affected_part_ids"],
                    )
                    await self.bus.publish(
                        ToolResultMsg(
                            agent_type=self.agent_type,
                            tool_name=tool_call.name,
                            result=correction.payload,
                            error=None,
                        )
                    )
                    result_str = self._format_tool_result(
                        correction.payload,
                        tool_name=tool_call.name,
                        tool_call_id=tool_call.id,
                    )
                except _ToolInputCorrection as correction:
                    logger.info(
                        "Incomplete tool input rejected for {} on agent {}; missing={}",
                        tool_call.name,
                        self.agent_type,
                        correction.payload["missing_argument_names"],
                    )
                    await self.bus.publish(
                        ToolResultMsg(
                            agent_type=self.agent_type,
                            tool_name=tool_call.name,
                            result=correction.payload,
                            error=None,
                        )
                    )
                    result_str = self._format_tool_result(
                        correction.payload,
                        tool_name=tool_call.name,
                        tool_call_id=tool_call.id,
                    )
                except asyncio.CancelledError:
                    if not self._cancel_event.is_set():
                        raise
                    logger.info(
                        "Tool execution cancelled for {} on agent {}",
                        tool_call.name,
                        self.agent_type,
                    )
                    error_msg = f"Interrupted while executing {tool_call.name}"
                    await self.bus.publish(
                        ToolResultMsg(
                            agent_type=self.agent_type,
                            tool_name=tool_call.name,
                            result=None,
                            error=error_msg,
                        )
                    )
                    result_str = error_msg
                except Exception as e:
                    logger.error(
                        "Tool execution error for {}: {} | args={}",
                        tool_call.name,
                        str(e),
                        tool_call.arguments,
                    )
                    error_msg = _sanitize_provider_visible_text(
                        f"Error executing {tool_call.name}: {str(e)}"
                    )

                    await self.bus.publish(
                        ToolResultMsg(
                            agent_type=self.agent_type,
                            tool_name=tool_call.name,
                            result=None,
                            error=error_msg,
                        )
                    )
                    result_str = error_msg

                # Add tool result as structured message
                # Anthropic API doesn't support 'tool' role, use 'user' instead
                provider_class_name = self.llm_provider.__class__.__name__
                tool_result_role = "user" if provider_class_name == "AnthropicProvider" else "tool"

                current_messages.append(
                    LLMMessage(
                        role=tool_result_role,
                        content=result_str,
                        tool_call_id=tool_call.id,
                        is_tool_result=True,
                    )
                )
                round_results.append(result_str)

                if self._terminal_outcome is not None:
                    for skipped_call in response.tool_calls[tool_index + 1 :]:
                        skipped = "Tool call skipped: a terminal result already ended this turn."
                        current_messages.append(
                            LLMMessage(
                                role=("user" if self.llm_provider.__class__.__name__ == "AnthropicProvider" else "tool"),
                                content=skipped,
                                tool_call_id=skipped_call.id,
                                is_tool_result=True,
                            )
                        )
                    self._conversation_history = [m for m in current_messages if m.role != "system"]
                    await self.bus.publish(
                        AgentResponse(
                            agent_type=self.agent_type,
                            content=canonical_terminal_message(self._terminal_outcome),
                            message_id=user_message_id,
                            streaming=False,
                            **self._active_workflow_correlation(),
                        )
                    )
                    return

                if self._cancel_event.is_set():
                    logger.info(
                        "Tool loop cancelled for agent {} after {}",
                        self.agent_type,
                        tool_call.name,
                    )
                    self._conversation_history = [
                        m for m in current_messages if m.role != "system"
                    ]
                    await self.bus.publish(
                        SystemNotice(
                            agent_type=self.agent_type,
                            content="Interrupted",
                            kind="interrupt",
                        )
                    )
                    await self._set_status(AgentStatus.IDLE)
                    return

                if self._turn_reported:
                    self._conversation_history = [
                        m for m in current_messages if m.role != "system"
                    ]
                    return

            if self._progress_monitor.observe(round_results) == "replan":
                reminder = (
                    "<progress_check>连续多轮工具结果没有新增证据、产物或状态变化。"
                    "不要重复相同调用；请基于现有结果提交、明确报告阻塞，或选择不同的"
                    "最小必要动作。此提示不代表任务已完成。</progress_check>"
                )
                current_messages.append(LLMMessage(role="user", content=reminder))
                await self.bus.publish(
                    SystemNotice(
                        agent_type=self.agent_type,
                        content="检测到重复工具结果，已要求 Agent 重新规划下一步。",
                    )
                )

            # Call LLM again with updated conversation
            await self._set_status(AgentStatus.THINKING)

            tool_definitions = self.tools.get_definitions()

            # Time the API call
            start_time = time.time()
            last_error = None
            response = _LoopLLMResponse(content="", tool_calls=[])

            try:
                current_messages = self._compact_working_memory(current_messages)
                response = await self._chat_with_retries(
                    current_messages,
                    tool_definitions,
                    user_message_id,
                    phase="tool_followup",
                    stream_idle_timeout_seconds=getattr(
                        self._current_message,
                        "provider_stream_idle_timeout_seconds",
                        None,
                    ),
                )
            except Exception as e:
                last_error = str(e)
                raise
            finally:
                # Calculate duration and publish debug info
                duration_ms = int((time.time() - start_time) * 1000)

                usage_record = self._last_usage_record or {
                    "input_tokens": 0,
                    "output_tokens": 0,
                }

                await self.bus.publish(
                    ApiDebugMessage(
                        model=self.llm_provider.model or "unknown",
                        tokens_in=usage_record["input_tokens"],
                        tokens_out=usage_record["output_tokens"],
                        duration_ms=duration_ms,
                        status="success" if last_error is None else "error",
                        error=last_error,
                    )
                )

            # This round is non-streaming, so thinking arrives as one block.
            # Publish it so the thinking indicator fills between tool calls —
            # without this the dots row is empty and gets removed on finish.
            if getattr(response, "thinking", None):
                # Streaming providers have already published the thinking
                # deltas. The fallback chat() path still needs one UI update.
                if not getattr(response, "streamed", False):
                    await self.bus.publish(
                        AgentResponse(
                            agent_type=self.agent_type,
                            content="",
                            message_id=user_message_id,
                            streaming=True,
                            thinking=response.thinking,
                            **self._active_workflow_correlation(),
                        )
                    )

            if self._cancel_event.is_set():
                self._conversation_history = [
                    m for m in current_messages if m.role != "system"
                ]
                await self.bus.publish(
                    SystemNotice(
                        agent_type=self.agent_type,
                        content="Interrupted",
                        kind="interrupt",
                    )
                )
                await self._set_status(AgentStatus.IDLE)
                return

        continuation_required = (
            iteration >= max_iterations
            and bool(response.tool_calls)
            and not self._turn_reported
        )

        # Update conversation history with final response
        if continuation_required:
            await self.bus.publish(
                AgentResponse(
                    agent_type=self.agent_type,
                    content=AGENT_TURN_CONTINUATION_REQUIRED,
                    message_id=user_message_id,
                    streaming=False,
                    internal=True,
                    **self._active_workflow_correlation(),
                )
            )
        elif response.content and not self._turn_reported:
            self._buffered_main_response = response.content
            current_messages.append(LLMMessage(role="assistant", content=response.content))
            if not self._must_buffer_main_report_route():
                # Publish final content to GUI (it was generated inside the tool loop)
                await self.bus.publish(
                    AgentResponse(
                        agent_type=self.agent_type,
                        content=response.content,
                        message_id=user_message_id,
                        streaming=False,
                        **self._active_workflow_correlation(),
                    )
                )

        # Save conversation history — strip the system prompt (index 0)
        self._conversation_history = [m for m in current_messages if m.role != "system"]

        if iteration >= max_iterations:
            logger.warning("Max tool iterations reached for agent: {}", self.agent_type)

    def _compact_working_memory(
        self, messages: list[LLMMessage]
    ) -> list[LLMMessage]:
        """Compact a model working set and persist the removed transcript."""

        compacted = _compact_messages_for_working_memory(
            messages,
            target_tokens=self.config.working_memory_tokens,
        )
        if compacted is messages:
            return messages

        retained_ids = {id(message) for message in compacted}
        removed = [message for message in messages if id(message) not in retained_ids]
        serialized = json.dumps(
            [
                {
                    "role": message.role,
                    "content": message.content or "",
                    "tool_call_id": message.tool_call_id,
                    "is_tool_result": message.is_tool_result,
                    "tool_calls": [
                        (
                            call.model_dump(mode="json")
                            if hasattr(call, "model_dump")
                            else {
                                "id": getattr(call, "id", None),
                                "name": getattr(call, "name", None),
                                "arguments": getattr(call, "arguments", None),
                            }
                        )
                        for call in (message.tool_calls or [])
                    ],
                }
                for message in removed
            ],
            ensure_ascii=False,
        )
        checkpoint_ref = self.artifact_gateway.persist_internal(
            "context-checkpoint", hashlib.sha256(serialized.encode()).hexdigest(), serialized
        )
        checkpoint = compacted[1]
        compacted[1] = LLMMessage(
            role=checkpoint.role,
            content=(checkpoint.content or "").replace(
                "Older tool transcripts were persisted locally.",
                (
                    "Older tool transcripts were persisted locally. "
                    f"checkpoint_ref={checkpoint_ref} "
                    f"checkpoint_sha256={hashlib.sha256(serialized.encode()).hexdigest()}"
                ),
            ),
        )
        logger.info(
            "Working memory compacted for {}: {} -> {} messages; checkpoint={}",
            self.agent_type,
            len(messages),
            len(compacted),
            checkpoint_ref,
        )
        return compacted

    def _record_token_usage(
        self,
        messages: list[LLMMessage],
        response: Any,
        *,
        phase: str,
        status: str,
        error: str | None,
        attempt: int = 1,
        retry: bool = False,
        attempt_disposition: str | None = None,
        retry_decision: str | None = None,
        guard: bool = False,
        error_class: str | None = None,
        tool_definitions: list[dict] | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        """Append one provider-round usage record, marking estimates explicitly."""

        raw_usage = getattr(response, "usage", None)
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        provider_input = usage.get("input_tokens", usage.get("prompt_tokens"))
        provider_output = usage.get("output_tokens", usage.get("completion_tokens"))
        if provider_input is not None and provider_output is not None:
            input_tokens = int(provider_input)
            output_tokens = int(provider_output)
            usage_source = "provider"
        else:
            input_tokens = _estimate_tokens(messages)
            output_tokens = _estimate_response_tokens(response)
            usage_source = "estimated"

        self._usage_totals["input_tokens"] += input_tokens
        self._usage_totals["output_tokens"] += output_tokens
        run_id = str(self.usage_run_id or self.agent_type)
        task_id = str(self.usage_task_id or "") or None
        serialization_started = time.perf_counter()
        message_payload = [
            {
                "role": message.role,
                "content": message.content or "",
                "tool_call_id": message.tool_call_id,
                "is_tool_result": bool(message.is_tool_result),
                "tool_calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    for call in (message.tool_calls or [])
                ],
            }
            for message in messages
        ]
        serialized_messages = json.dumps(
            message_payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        serialized_tools = json.dumps(
            tool_definitions or [],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        serialization_ms = int(
            (time.perf_counter() - serialization_started) * 1000
        )
        pre_adapter_request_fingerprint = hashlib.sha256(
            f"{serialized_messages}\n{serialized_tools}".encode("utf-8")
        ).hexdigest()
        pre_adapter_message_fingerprint = hashlib.sha256(
            serialized_messages.encode("utf-8")
        ).hexdigest()
        pre_adapter_tool_schema_fingerprint = hashlib.sha256(
            serialized_tools.encode("utf-8")
        ).hexdigest()
        request_metrics = getattr(response, "request_metrics", None)
        if not isinstance(request_metrics, dict):
            request_metrics = {}
        provider_request_observed = bool(
            request_metrics.get("request_fingerprint")
            and request_metrics.get("message_fingerprint")
            and request_metrics.get("tool_schema_fingerprint")
        )
        request_fingerprint = str(
            request_metrics.get("request_fingerprint")
            or pre_adapter_request_fingerprint
        )
        message_fingerprint = str(
            request_metrics.get("message_fingerprint")
            or pre_adapter_message_fingerprint
        )
        tool_schema_fingerprint = str(
            request_metrics.get("tool_schema_fingerprint")
            or pre_adapter_tool_schema_fingerprint
        )
        message_chars = int(
            request_metrics.get("message_chars", len(serialized_messages))
            or 0
        )
        tool_schema_chars = int(
            request_metrics.get("tool_schema_chars", len(serialized_tools))
            or 0
        )
        prompt_details = usage.get("prompt_tokens_details")
        if not isinstance(prompt_details, dict):
            prompt_details = {}
        cached_input_tokens = int(
            usage.get(
                "cache_read_input_tokens",
                usage.get("cached_input_tokens", prompt_details.get("cached_tokens", 0)),
            )
            or 0
        )
        cache_write_input_tokens = int(
            usage.get(
                "cache_creation_input_tokens",
                usage.get("cache_write_input_tokens", 0),
            )
            or 0
        )
        uncached_input_tokens = max(
            0,
            input_tokens - cached_input_tokens - cache_write_input_tokens,
        )
        context_manifest_ref = getattr(
            self,
            "usage_context_manifest_ref",
            None,
        )
        provider_call_id = getattr(
            self,
            "usage_provider_call_id",
            None,
        )
        if not provider_call_id and context_manifest_ref:
            provider_call_id = Path(str(context_manifest_ref)).stem
        phase_turn_kind = (
            "guard"
            if guard or phase.startswith("guard")
            else "tool_followup"
            if phase == "tool_followup"
            else str(
                getattr(
                    self._current_message,
                    "turn_kind",
                    "task_initial",
                )
            )
        )
        queue_wait_ms = self._usage_queue_wait_ms
        context_build_ms = self._usage_context_build_ms
        tool_time_ms = self._usage_tool_time_ms
        self._usage_queue_wait_ms = 0
        self._usage_context_build_ms = 0
        self._usage_tool_time_ms = 0
        record = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "run_id": run_id,
            "task_id": task_id,
            "agent_id": str(self.agent_type),
            "stage": str(getattr(self, "usage_stage", "") or phase),
            "phase": phase,
            "turn_kind": phase_turn_kind,
            "provider": self.llm_provider.__class__.__name__,
            "model": self.llm_provider.model or "unknown",
            "status": status,
            "error": error,
            "error_class": error_class,
            "attempt": attempt,
            "retry": retry,
            "attempt_disposition": attempt_disposition,
            "retry_decision": retry_decision,
            "guard": guard or phase.startswith("guard"),
            "message_count": len(messages),
            "message_chars": message_chars,
            "tool_schema_chars": tool_schema_chars,
            "request_chars": int(
                request_metrics.get(
                    "request_chars",
                    len(serialized_messages) + len(serialized_tools),
                )
                or 0
            ),
            "request_fingerprint": request_fingerprint,
            "message_fingerprint": message_fingerprint,
            "tool_schema_fingerprint": tool_schema_fingerprint,
            "request_metric_source": (
                "provider_adapter_payload"
                if provider_request_observed
                else "agent_pre_adapter"
            ),
            "provider_request_representation": request_metrics.get(
                "representation"
            ),
            "pre_adapter_request_fingerprint": pre_adapter_request_fingerprint,
            "pre_adapter_message_fingerprint": pre_adapter_message_fingerprint,
            "pre_adapter_tool_schema_fingerprint": (
                pre_adapter_tool_schema_fingerprint
            ),
            "pre_adapter_message_chars": len(serialized_messages),
            "pre_adapter_tool_schema_chars": len(serialized_tools),
            "provider_call_id": provider_call_id,
            "context_manifest_ref": context_manifest_ref,
            "response_tool_call_count": len(
                getattr(response, "tool_calls", None) or []
            ),
            "duration_ms": duration_ms,
            "queue_wait_ms": queue_wait_ms,
            "context_build_ms": context_build_ms,
            "serialization_ms": serialization_ms,
            "ttft_ms": getattr(response, "ttft_ms", None),
            "provider_active_ms": (
                getattr(response, "provider_active_ms", None)
                if getattr(response, "provider_active_ms", None) is not None
                else duration_ms
            ),
            "tool_time_ms": tool_time_ms,
            "execution_profile_id": getattr(
                self, "usage_execution_profile_id", None
            ),
            "execution_profile_version": getattr(
                self, "usage_execution_profile_version", None
            ),
            "execution_profile_sha256": getattr(
                self, "usage_execution_profile_sha256", None
            ),
            "resolved_provider_route": getattr(
                self, "usage_resolved_provider_route", "inherit"
            ),
            "resolved_model": getattr(
                self,
                "usage_resolved_model",
                self.llm_provider.model or "unknown",
            ),
            "task_priority": getattr(self, "usage_task_priority", None),
            "expected_duration_ms": getattr(
                self, "usage_expected_duration_ms", None
            ),
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "cache_write_input_tokens": cache_write_input_tokens,
            "uncached_input_tokens": uncached_input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "usage_source": usage_source,
            "agent_cumulative_input_tokens": self._usage_totals["input_tokens"],
            "agent_cumulative_output_tokens": self._usage_totals["output_tokens"],
        }
        return UsageLedger(self.workspace, run_id).record_attempt(**record)

    def _format_tool_result(
        self,
        result: Any,
        *,
        tool_name: str = "tool",
        tool_call_id: str = "result",
    ) -> str:
        """Format tool result for LLM.

        Args:
            result: Tool result.

        Returns:
            Formatted result string.
        """
        if isinstance(result, dict):
            filtered = {
                key: value
                for key, value in result.items()
                if not str(key).startswith("_ui_") and str(key) != "completion_summary"
            }
            rendered = json.dumps(filtered, ensure_ascii=False, default=str)
        elif isinstance(result, str):
            rendered = result
        else:
            rendered = json.dumps(result, ensure_ascii=False, default=str)

        rendered = _sanitize_provider_visible_text(rendered)

        max_chars = self.config.max_tool_result_chars
        if len(rendered) <= max_chars:
            return rendered

        safe_agent = "".join(
            char if char.isalnum() or char in "-_." else "_"
            for char in str(self.agent_type)
        )
        safe_call = "".join(
            char if char.isalnum() or char in "-_." else "_"
            for char in str(tool_call_id)
        )
        opaque_ref = self.artifact_gateway.persist_internal(
            "tool-result", f"{safe_agent}:{safe_call}", rendered
        )
        preview_chars = max(0, max_chars - 1024)
        while True:
            preview = rendered[:preview_chars]
            compact = {
                "truncated": True,
                "tool": tool_name,
                "full_result_ref": opaque_ref,
                "original_chars": len(rendered),
                "preview_offset": 0,
                "preview_chars": len(preview),
                "preview_end_offset": len(preview),
                "next_offset": len(preview),
                "recommended_limit": 8000,
                "instruction": (
                    "如需剩余内容，调用 open_tool_result，ref 使用 full_result_ref，"
                    "offset 使用 next_offset，省略 limit 即按 8000 字符读取；"
                    "不得从 offset=0 重读 preview。"
                ),
                "preview": preview,
            }
            encoded = json.dumps(compact, ensure_ascii=False)
            if len(encoded) <= max_chars or preview_chars == 0:
                return encoded
            preview_chars = max(0, preview_chars - (len(encoded) - max_chars))

    async def _set_status(self, status: AgentStatus) -> None:
        """Set agent status and notify GUI via bus.

        Args:
            status: New agent status.
        """
        self._status = status
        if status in {AgentStatus.IDLE, AgentStatus.ERROR}:
            self._turn_complete_event.set()
        else:
            self._turn_complete_event.clear()

        # In debug mode, use DEBUG_MODE status
        display_status = AgentStatus.DEBUG_MODE if self._debug_mode else status

        await self.bus.publish(
            StatusChange(
                agent_type=self.agent_type,
                status=display_status,
            )
        )

    def set_debug_mode(self, enabled: bool) -> None:
        """Enable or disable debug mode.

        When enabled, unsubscribes from the MessageBus entirely so the
        agent only processes direct user input (no Main Agent coordination).
        When disabled, re-subscribes to the bus.

        Args:
            enabled: Whether debug mode is enabled.
        """
        self._debug_mode = enabled

        if enabled:
            self.bus.unsubscribe(UserMessage, self._bus_callback)
            logger.info("Debug mode enabled for agent: {} (unsubscribed from bus)", self.agent_type)
        else:
            self.bus.subscribe(UserMessage, self._bus_callback)
            logger.info("Debug mode disabled for agent: {} (re-subscribed to bus)", self.agent_type)

    @property
    def debug_mode(self) -> bool:
        """Check if debug mode is enabled.

        Returns:
            True if debug mode is enabled.
        """
        return self._debug_mode

    async def _get_cached_system_prompt(self) -> str:
        """Return the cached system prompt, rebuilding it when sources change.

        The prompt is assembled from the runtime prompt and shared context.
        The assembled text is cached, but the cache is invalidated
        automatically when the underlying prompt files change.
        """
        if self._system_prompt_override is not None:
            return self._system_prompt_override

        agent_type_str = self._get_agent_type_str()
        logger.debug("Loading system prompt for agent: {}", self.agent_type)
        prompt_signature = self._prompt_loader.get_signature(agent_type_str)
        current_signature = (
            agent_type_str,
            prompt_signature,
            bool(self._manifest_manager and self.agent_id != "main"),
        )

        if (
            self._cached_system_prompt is not None
            and self._cached_system_prompt_signature == current_signature
        ):
            return self._cached_system_prompt

        parts: list[str] = [self._prompt_loader.load_prompt(agent_type_str)]

        shared = self._prompt_loader.load_shared_context()
        if shared:
            parts.append(shared)

        # Manifest hint — sub-agents only
        if self._manifest_manager and self.agent_id != "main":
            parts.append(
                "\n\n[Manifest]\n"
                "你可以在需要时使用 manifest 了解当前本地提供了哪些文件。"
                "文件描述应简短，更复杂的关系、依赖、协作说明写在自由文本区。"
                "只有在本轮结束前，才重点补充 manifest 的自由文本区。"
            )

        self._cached_system_prompt = "\n\n".join(parts)
        self._cached_system_prompt_signature = current_signature
        return self._cached_system_prompt

    async def _get_system_prompt(self) -> str:
        """Return the system prompt with live task state appended.

        The base prompt is cached once; the task-state section is rebuilt
        fresh each time so it reflects the current board state.
        """
        base = await self._get_cached_system_prompt()
        task_section = self._build_task_state_section()
        if task_section:
            return base + task_section
        return base

    async def _build_messages(self) -> list[LLMMessage]:
        """Build the message list for any LLM request.

        System prompt is always first (with cache_control for Anthropic caching),
        followed by the recorded conversation history.  This single entry-point
        is used by both the initial streaming call *and* tool-call iterations, so
        the system prompt is never accidentally dropped.
        """
        sanitized_history = [
            LLMMessage(
                role=msg.role,
                content=msg.content or "",
                tool_calls=msg.tool_calls,
                tool_call_id=msg.tool_call_id,
                is_tool_result=msg.is_tool_result,
                thinking=msg.thinking,
                cache_control=msg.cache_control,
            )
            for msg in self._conversation_history
        ]
        return [
            LLMMessage(role="system", content=await self._get_system_prompt(), cache_control=True),
            *sanitized_history,
        ]

    def _build_task_state_section(self) -> str:
        """Build current task state section for the system prompt.

        Rules:
        - Incomplete tasks: show all
        - Completed tasks: show only 10 most recently completed (by completed_at)
        """
        task_board = self._task_board
        if task_board is None and self._loop_manager is not None:
            task_board = getattr(self._loop_manager, "_task_board", None)
        if task_board is None:
            return ""

        def _merge_by_identity(primary, fallback):
            seen = set()
            merged = []
            for task in [*primary, *fallback]:
                key = (task.task_id, task.source_agent, task.target_agent)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(task)
            return merged

        todolist = _merge_by_identity(
            task_board.get_todolist(self.agent_type, session_id=self._current_session_id),
            task_board.get_todolist(self.agent_type),
        )
        waitlist = _merge_by_identity(
            task_board.get_waitlist(self.agent_type, session_id=self._current_session_id),
            task_board.get_waitlist(self.agent_type),
        )

        if not todolist and not waitlist:
            return ""

        lines = ["\n[当前任务]"]

        # Helper to format task list
        def format_task_list(tasks, is_waitlist: bool = False) -> list[str]:
            if not tasks:
                return []

            # Separate incomplete and completed
            incomplete = [t for t in tasks if t.status != TaskStatus.COMPLETED]
            completed = [t for t in tasks if t.status == TaskStatus.COMPLETED]

            result = []

            # Show all incomplete tasks first
            for t in incomplete:
                if is_waitlist:
                    tgt = t.target_agent
                    result.append(f"  - task_id={t.task_id} 等待{tgt} {t.brief}")
                else:
                    status_map = {
                        TaskStatus.PENDING: "待处理",
                        TaskStatus.IN_PROGRESS: "进行中",
                        TaskStatus.FAILED: "失败",
                        TaskStatus.CANCELLED: "已取消",
                    }
                    s = status_map.get(t.status, t.status.value)
                    result.append(f"  - task_id={t.task_id} {s} {t.brief}")

            # Show 10 most recently completed tasks
            if completed:
                # Sort by completed_at descending (most recent first)
                completed_sorted = sorted(
                    completed, key=lambda t: t.completed_at or datetime.min, reverse=True
                )[:10]

                for t in completed_sorted:
                    if is_waitlist:
                        tgt = t.target_agent
                        result.append(f"  - task_id={t.task_id} 等待{tgt} {t.brief} (已完成)")
                    else:
                        result.append(f"  - task_id={t.task_id} 已完成 {t.brief}")

            return result

        if todolist:
            lines.append("待办:")
            lines.extend(format_task_list(todolist, is_waitlist=False))

        if waitlist:
            lines.append("等待:")
            lines.extend(format_task_list(waitlist, is_waitlist=True))

        return "\n".join(lines)

    def _get_agent_type_str(self) -> str:
        """Return this loop's registry identifier for prompt loading.

        Returns:
            Agent type string identifier.
        """
        return self.agent_id
