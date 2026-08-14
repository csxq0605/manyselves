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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Mapping, Sequence

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
from ..mimo_pricing import (
    calculate_mimo_v25_pro_run_cost,
    format_mimo_v25_pro_cost,
)
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

# A handoff summary is deliberately a normal model-facing message.  Older
# compaction used a first-message snippet plus regex-extracted ids and a
# ``checkpoint_ref``/``open_tool_result`` escape hatch.  That made compaction
# depend on incidental prose and encouraged the model to reopen old output.  A
# bounded structured handoff instead carries the durable decision state and
# the current task boundary while the lossless transcript remains local.
_HANDOFF_SUMMARY_MARKER = "<context_handoff_summary>"
_HANDOFF_SUMMARY_END = "</context_handoff_summary>"

# Reaching a bounded tool slice is not a terminal Agent state. Reporting
# orchestration uses this signal to continue with the same durable identity.
AGENT_TURN_CONTINUATION_REQUIRED = "AGENT_TURN_CONTINUATION_REQUIRED"
AGENT_MAX_TOKENS_CONTINUATION_REQUIRED = (
    "AGENT_MAX_TOKENS_CONTINUATION_REQUIRED"
)
# Deterministic local outcome used when a typed reporting context cannot fit
# the Provider window even after a lossless rebase.  It is intentionally not a
# Provider error: no network request was sent and callers may persist a legal
# continuation/blocked state from the current typed capsule.
CONTEXT_BUDGET_EXHAUSTED = "context_budget_exhausted"

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


def _report_workflow_operation(workspace: Path, payload: dict[str, Any]) -> str | None:
    """Return the persisted operation for one controller-owned report terminal."""

    run_id = str(payload.get("run_id", "") or "")
    if not run_id.startswith("report-") or Path(run_id).name != run_id:
        return None
    request_path = workspace / "Work" / "runs" / run_id / "request.json"
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(request, dict):
        return None
    operation = str(request.get("operation", "") or "").strip()
    return operation or None


def _canonical_completed_report_response(
    workspace: Path,
    message: UserMessage | None,
) -> str | None:
    """Deliver a successful report terminal without another Main planning round."""

    payload = _report_workflow_terminal_payload(message)
    if payload is None or payload.get("status") not in {"completed", "delivered"}:
        return None
    if _report_workflow_operation(workspace, payload) == "distill_template_skill":
        return None
    run_id = str(payload["run_id"])
    raw_paths = payload.get("output_paths")
    output_paths = (
        [str(path).strip() for path in raw_paths if str(path).strip()]
        if isinstance(raw_paths, (list, tuple))
        else []
    )
    existing_paths: list[str] = []
    missing_paths: list[str] = []
    for raw_path in output_paths:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            candidate = candidate.resolve()
            candidate.relative_to(workspace)
        except (OSError, ValueError):
            missing_paths.append(raw_path)
            continue
        if candidate.is_file():
            existing_paths.append(raw_path)
        else:
            missing_paths.append(raw_path)
    if not output_paths or missing_paths:
        details = (
            f"缺失或无效输出：{', '.join(missing_paths)}。"
            if missing_paths
            else "终态没有提供输出文件。"
        )
        return (
            f"报告任务 {run_id} 虽返回成功终态，但交付校验未通过：{details}\n\n"
            "本轮不会启动新的报告运行；请先检查该 run 的交付状态。"
        )
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    usage_line = ""
    if usage:
        metrics = []
        if isinstance(usage.get("provider_attempts"), int):
            metrics.append(f"Provider 调用 {usage['provider_attempts']} 次")
        if isinstance(usage.get("total_tokens"), int):
            metrics.append(f"总 Token {usage['total_tokens']:,}")
        if metrics:
            usage_line = "\n\n用量：" + "，".join(metrics) + "。"
    mimo_cost = calculate_mimo_v25_pro_run_cost(workspace, run_id)
    if mimo_cost is not None:
        usage_line += "\n\n" + format_mimo_v25_pro_cost(mimo_cost)
    outputs = "\n".join(f"- `{path}`" for path in existing_paths)
    return (
        f"报告任务 {run_id} 已成功完成并交付。\n\n输出：\n{outputs}"
        f"{usage_line}\n\n本轮已结束，不会启动新的报告运行。"
    )


def _report_workflow_operation(workspace: Path, payload: dict[str, Any]) -> str | None:
    """Return the persisted operation for one controller-owned report terminal."""

    run_id = str(payload.get("run_id", "") or "")
    if (
        not run_id.startswith("report-")
        or Path(run_id).name != run_id
    ):
        return None
    request_path = workspace / "Work" / "runs" / run_id / "request.json"
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(request, dict):
        return None
    operation = str(request.get("operation", "") or "").strip()
    return operation or None


def _canonical_completed_report_response(
    workspace: Path,
    message: UserMessage | None,
) -> str | None:
    """Deliver a successful report terminal without another Main planning round."""

    payload = _report_workflow_terminal_payload(message)
    if payload is None or payload.get("status") not in {"completed", "delivered"}:
        return None
    if _report_workflow_operation(workspace, payload) == "distill_template_skill":
        # Template distillation may be the first explicitly requested step of a
        # multi-step user request. Main may advance to the requested writing run.
        return None

    run_id = str(payload["run_id"])
    raw_paths = payload.get("output_paths")
    output_paths = [
        str(path).strip()
        for path in raw_paths
        if str(path).strip()
    ] if isinstance(raw_paths, (list, tuple)) else []
    existing_paths: list[str] = []
    missing_paths: list[str] = []
    for raw_path in output_paths:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            candidate = candidate.resolve()
            candidate.relative_to(workspace)
        except (OSError, ValueError):
            missing_paths.append(raw_path)
            continue
        if candidate.is_file():
            existing_paths.append(raw_path)
        else:
            missing_paths.append(raw_path)

    if not output_paths or missing_paths:
        details = (
            f"缺失或无效输出：{', '.join(missing_paths)}。"
            if missing_paths
            else "终态没有提供输出文件。"
        )
        return (
            f"报告任务 {run_id} 虽返回成功终态，但交付校验未通过：{details}\n\n"
            "本轮不会启动新的报告运行；请先检查该 run 的交付状态。"
        )

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    usage_line = ""
    if usage:
        attempts = usage.get("provider_attempts")
        total_tokens = usage.get("total_tokens")
        metrics = []
        if isinstance(attempts, int):
            metrics.append(f"Provider 调用 {attempts} 次")
        if isinstance(total_tokens, int):
            metrics.append(f"总 Token {total_tokens:,}")
        if metrics:
            usage_line = "\n\n用量：" + "，".join(metrics) + "。"

    mimo_cost = calculate_mimo_v25_pro_run_cost(workspace, run_id)
    if mimo_cost is not None:
        usage_line += "\n\n" + format_mimo_v25_pro_cost(mimo_cost)

    outputs = "\n".join(f"- `{path}`" for path in existing_paths)
    return (
        f"报告任务 {run_id} 已成功完成并交付。\n\n输出：\n{outputs}"
        f"{usage_line}\n\n本轮已结束，不会启动新的报告运行。"
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


@dataclass(frozen=True)
class ContextGateDecision:
    """Pure pre-send decision for one logical Provider round.

    ``allow`` means the supplied context can be sent as-is.  ``rebuilt`` is
    returned when a reporting rebaser supplied a new Provider view.  ``blocked``
    is fail-closed: the caller must not invoke a Provider.  Character counters
    are telemetry only and never affect whether a request is charged.
    """

    status: Literal["allow", "rebuilt", "blocked"]
    reason: str = ""
    provider_request_sent: bool = False
    duplicate_tool_result_chars: int = 0
    duplicate_completed_result_chars: int = 0
    duplicate_evidence_chars: int = 0
    rebuild_count: int = 0


def _context_message_payload(message: Any) -> dict[str, Any]:
    """Build a deterministic, content-addressable message representation."""

    calls = []
    for call in getattr(message, "tool_calls", None) or ():
        calls.append(
            {
                "id": str(getattr(call, "id", "") or ""),
                "name": str(getattr(call, "name", "") or ""),
                "arguments": getattr(call, "arguments", {}) or {},
            }
        )
    return {
        "role": str(getattr(message, "role", "") or ""),
        "content": str(getattr(message, "content", "") or ""),
        "tool_call_id": str(getattr(message, "tool_call_id", "") or ""),
        "is_tool_result": bool(getattr(message, "is_tool_result", False)),
        "tool_calls": calls,
    }


def _context_payload_fingerprint(
    messages: Sequence[Any], tool_definitions: Sequence[dict[str, Any]] | None = None
) -> str:
    payload = {
        "messages": [_context_message_payload(message) for message in messages],
        "tools": list(tool_definitions or ()),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _context_result_signatures(messages: Sequence[Any]) -> dict[str, tuple[str, int]]:
    """Return tool-result id -> (content hash, character count)."""

    results: dict[str, tuple[str, int]] = {}
    for message in messages:
        if not (
            bool(getattr(message, "is_tool_result", False))
            or str(getattr(message, "role", "") or "") == "tool"
        ):
            continue
        call_id = str(getattr(message, "tool_call_id", "") or "")
        if not call_id:
            continue
        content = str(getattr(message, "content", "") or "")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        results.setdefault(call_id, (digest, len(content)))
    return results


def _context_completed_prose_signatures(messages: Sequence[Any]) -> dict[str, tuple[int, int]]:
    """Find durable completed prose repeated in a Provider payload.

    This deliberately recognises structured status fields instead of filename
    or keyword guesses.  A result marked ``persisted``/``completed`` is a
    durable write and must not be replayed as a fresh write in the same context.
    """

    found: dict[str, tuple[int, int]] = {}
    for message in messages:
        content = str(getattr(message, "content", "") or "")
        if not content or not re.search(
            r"(?:\"(?:persisted|status|ready)\"\s*:\s*(?:true|\"completed\"|true))",
            content,
            flags=re.IGNORECASE,
        ):
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        chars, count = found.get(digest, (0, 0))
        found[digest] = (chars + len(content), count + 1)
    return found


def _context_evidence_signatures(messages: Sequence[Any]) -> dict[str, tuple[int, int]]:
    """Find repeated hash-addressed evidence refs in one Provider payload."""

    found: dict[str, tuple[int, int]] = {}
    digest_pattern = re.compile(r"(?:sha256|content_sha256|result_sha256)\s*[=:]\s*([0-9a-f]{64})", re.IGNORECASE)
    for message in messages:
        content = str(getattr(message, "content", "") or "")
        if not content:
            continue
        for digest in digest_pattern.findall(content):
            chars, count = found.get(digest, (0, 0))
            found[digest] = (chars + len(content), count + 1)
    return found


def _context_reference_only(messages: Sequence[Any]) -> bool:
    """Whether all non-system content is a typed/ref/hash carrier.

    Stable prompts and immutable references are safe to repeat.  This helper
    intentionally does not classify ordinary prose by filename or keywords.
    """

    for message in messages:
        role = str(getattr(message, "role", "") or "")
        content = str(getattr(message, "content", "") or "").strip()
        if role == "system" or not content:
            continue
        if getattr(message, "tool_calls", None) or getattr(message, "is_tool_result", False):
            return False
        if content.startswith(("<typed_task_state>", "<bounded_context>")):
            continue
        if re.fullmatch(r"(?:ref|sha256|content_sha256|result_sha256)\s*[=:].+", content, re.IGNORECASE):
            continue
        # Hash-only JSON/object carriers contain no reader-visible prose.
        if re.fullmatch(r"[\[{].*(?:sha256|content_sha256|ref).*[\]}]", content, re.IGNORECASE | re.DOTALL):
            continue
        return False
    return True


def pre_send_context_gate(
    messages: Sequence[Any],
    *,
    previous_messages: Sequence[Any] | None = None,
    tool_definitions: Sequence[dict[str, Any]] | None = None,
    previous_tool_definitions: Sequence[dict[str, Any]] | None = None,
    phase: str = "initial",
    attempt: int = 1,
    previous_attempt_disposition: str | None = None,
    rebuilt: bool = False,
    rebuild_count: int = 0,
) -> ContextGateDecision:
    """Pure fail-closed gate used immediately before a Provider request.

    The gate blocks exact replay and duplicate durable units, while retaining
    explicit allowances for safe no-output retries and typed follow-up rounds.
    It never performs I/O or invokes a Provider, which makes it suitable for
    deterministic fake-provider tests.
    """

    current = list(messages)
    previous = list(previous_messages or ())
    safe_retry = (
        int(attempt or 1) > 1
        and str(previous_attempt_disposition or "").casefold()
        in {"not_sent", "definitely_rejected"}
    )
    current_results = _context_result_signatures(current)
    previous_results = _context_result_signatures(previous)

    # Duplicate ids/content inside one request are always malformed.  Normal
    # conversation replay carries each completed result exactly once.  Keep
    # malformed in-request duplicates separate from a complete prior unit that
    # a typed continuation intentionally carries forward: the latter is an
    # atomic prerequisite and is legal once a new logical round is explicit.
    duplicate_result_chars = 0
    duplicate_in_request_result_chars = 0
    raw_result_ids = [
        str(getattr(message, "tool_call_id", "") or "")
        for message in current
        if bool(getattr(message, "is_tool_result", False))
        or str(getattr(message, "role", "") or "") == "tool"
    ]
    duplicate_ids = {call_id for call_id in raw_result_ids if raw_result_ids.count(call_id) > 1 and call_id}
    duplicate_in_request_result_chars += sum(
        chars for call_id, (_digest, chars) in current_results.items() if call_id in duplicate_ids
    )
    duplicate_result_chars += duplicate_in_request_result_chars

    # Re-sending an already complete result while adding no new result is a
    # replay, even if a harness-owned reminder changed the surrounding prose.
    if previous_results and current_results:
        shared = {
            call_id
            for call_id in set(current_results).intersection(previous_results)
            if current_results[call_id] == previous_results[call_id]
        }
        current_new = set(current_results).difference(previous_results)
        if shared and not current_new:
            replayed_result_chars = sum(current_results[item][1] for item in shared)
            duplicate_result_chars += replayed_result_chars
        else:
            replayed_result_chars = 0
    else:
        replayed_result_chars = 0

    completed = _context_completed_prose_signatures(current)
    duplicate_completed_chars = sum(chars for chars, count in completed.values() if count > 1)
    previous_completed = _context_completed_prose_signatures(previous)
    if previous_completed:
        duplicate_completed_chars += sum(
            chars for digest, (chars, _count) in completed.items() if digest in previous_completed
        )

    evidence = _context_evidence_signatures(current)
    duplicate_evidence_chars = sum(chars for chars, count in evidence.values() if count > 1)
    previous_evidence = _context_evidence_signatures(previous)
    if previous_evidence:
        duplicate_evidence_chars += sum(
            chars for digest, (chars, _count) in evidence.items() if digest in previous_evidence
        )

    current_fingerprint = _context_payload_fingerprint(current, tool_definitions)
    previous_fingerprint = _context_payload_fingerprint(previous, previous_tool_definitions)
    exact_replay = bool(previous and current_fingerprint == previous_fingerprint)
    reference_only = _context_reference_only(current)
    phase_text = str(phase or "initial").casefold()
    explicit_followup = (
        phase_text == "tool_followup"
        or "evidence" in phase_text
        or "continuation" in phase_text
        or "correction" in phase_text
        or "revision" in phase_text
        or phase_text.startswith("guard")
    )
    # AgentLoop receives continuation/correction turns as ``phase='initial'``
    # because each Runner ``one_turn`` is a fresh UserMessage.  Only the
    # newest non-tool user message can authorize replay; looking through the
    # whole transcript would let an old boundary marker bless an exact replay.
    latest_user_content = next(
        (
            str(getattr(message, "content", "") or "")
            for message in reversed(current)
            if str(getattr(message, "role", "") or "") == "user"
            and not bool(getattr(message, "is_tool_result", False))
        ),
        "",
    )
    explicit_followup = explicit_followup or any(
        marker in latest_user_content
        for marker in ("<same_identity_continuation>", "<submission_correction>")
    )
    if duplicate_in_request_result_chars:
        return ContextGateDecision(
            "blocked",
            "duplicate_complete_tool_result",
            duplicate_tool_result_chars=duplicate_result_chars,
            duplicate_completed_result_chars=duplicate_completed_chars,
            duplicate_evidence_chars=duplicate_evidence_chars,
            rebuild_count=max(0, int(rebuild_count or 0)),
        )
    if replayed_result_chars and not (safe_retry or explicit_followup):
        return ContextGateDecision(
            "blocked",
            "duplicate_complete_tool_result",
            duplicate_tool_result_chars=duplicate_result_chars,
            duplicate_completed_result_chars=duplicate_completed_chars,
            duplicate_evidence_chars=duplicate_evidence_chars,
            rebuild_count=max(0, int(rebuild_count or 0)),
        )
    if duplicate_completed_chars and not (safe_retry or explicit_followup or reference_only):
        return ContextGateDecision(
            "blocked",
            "duplicate_completed_result",
            duplicate_completed_result_chars=duplicate_completed_chars,
            duplicate_evidence_chars=duplicate_evidence_chars,
            rebuild_count=max(0, int(rebuild_count or 0)),
        )
    if duplicate_evidence_chars and not (safe_retry or explicit_followup or reference_only):
        return ContextGateDecision(
            "blocked",
            "duplicate_consumed_evidence",
            duplicate_evidence_chars=duplicate_evidence_chars,
            rebuild_count=max(0, int(rebuild_count or 0)),
        )
    if exact_replay and not (safe_retry or explicit_followup or reference_only):
        return ContextGateDecision(
            "blocked",
            "duplicate_provider_context",
            rebuild_count=max(0, int(rebuild_count or 0)),
        )
    if rebuilt:
        return ContextGateDecision(
            "rebuilt",
            "typed_context_rebased",
            duplicate_tool_result_chars=duplicate_result_chars,
            duplicate_completed_result_chars=duplicate_completed_chars,
            duplicate_evidence_chars=duplicate_evidence_chars,
            rebuild_count=max(1, int(rebuild_count or 0)),
        )
    if safe_retry:
        return ContextGateDecision(
            "allow",
            "safe_no_output_retry",
            duplicate_tool_result_chars=duplicate_result_chars,
            duplicate_completed_result_chars=duplicate_completed_chars,
            duplicate_evidence_chars=duplicate_evidence_chars,
            rebuild_count=0,
        )
    if explicit_followup:
        return ContextGateDecision(
            "allow",
            "typed_followup",
            duplicate_tool_result_chars=duplicate_result_chars,
            duplicate_completed_result_chars=duplicate_completed_chars,
            duplicate_evidence_chars=duplicate_evidence_chars,
            rebuild_count=0,
        )
    return ContextGateDecision(
        "allow",
        "new_context",
        duplicate_tool_result_chars=duplicate_result_chars,
        duplicate_completed_result_chars=duplicate_completed_chars,
        duplicate_evidence_chars=duplicate_evidence_chars,
        rebuild_count=0,
    )


# Descriptive alias used by small offline harnesses and downstream callers.
evaluate_pre_send_context_gate = pre_send_context_gate


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


def _tool_correction_payload(message: LLMMessage) -> dict[str, Any] | None:
    """Return one rejected tool-call correction carried by a tool result."""

    if not message.is_tool_result or not message.content:
        return None
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        str(payload.get("status") or "").strip().casefold()
        != "correction_required"
        or payload.get("accepted") is not False
    ):
        return None
    return payload


def _provider_working_messages(
    messages: list[LLMMessage],
) -> list[LLMMessage]:
    """Remove rejected tool calls from the Provider-visible repair history.

    The lossless internal transcript keeps the original tool call and paired
    correction result for forensics.  Replaying that rejected call to the model,
    however, turns its invalid arguments into an in-context example and makes a
    repair turn likely to copy the same shape.  Provider working history therefore
    keeps successful tool protocol pairs but converts rejected pairs into ordinary
    user repair instructions with no malformed call arguments attached.
    """

    working: list[LLMMessage] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role != "assistant" or not message.tool_calls:
            working.append(message)
            index += 1
            continue

        result_index = index + 1
        tool_results: list[LLMMessage] = []
        while result_index < len(messages) and messages[result_index].is_tool_result:
            tool_results.append(messages[result_index])
            result_index += 1
        results_by_id = {
            result.tool_call_id: result
            for result in tool_results
            if result.tool_call_id is not None
        }
        rejected: dict[str, dict[str, Any]] = {}
        for call in message.tool_calls:
            result = results_by_id.get(call.id)
            correction = (
                _tool_correction_payload(result) if result is not None else None
            )
            if correction is not None:
                rejected[call.id] = correction
        if not rejected:
            working.append(message)
            working.extend(tool_results)
            index = result_index
            continue

        retained_calls = [
            call for call in message.tool_calls if call.id not in rejected
        ]
        if message.content or retained_calls:
            working.append(
                LLMMessage(
                    role="assistant",
                    content=message.content,
                    tool_calls=retained_calls or None,
                    thinking=message.thinking,
                    cache_control=message.cache_control,
                )
            )
        working.extend(
            result
            for result in tool_results
            if result.tool_call_id not in rejected
        )
        for call in message.tool_calls:
            if call.id not in rejected:
                continue
            correction_result = results_by_id[call.id]
            working.append(
                LLMMessage(
                    role="user",
                    content=(
                        f'<tool_input_correction tool_name="{call.name}">\n'
                        "The rejected tool call was removed from working history so "
                        "its invalid arguments are not an example to copy. Rebuild the "
                        "call from the current tool schema and apply this feedback:\n"
                        f"{correction_result.content}\n"
                        "</tool_input_correction>"
                    ),
                )
            )
        index = result_index

    return working


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

    # Always keep system message (index 0). Group the rest into protocol
    # atomic units so compaction never manufactures an orphan tool result or a
    # partial assistant tool-call example.
    system_msg = messages[0]
    rest = messages[1:]
    units = _atomic_history_units(rest)
    if not units:
        return [system_msg]

    # The latest complete unit is the active task boundary and must survive;
    # admit older complete units newest-first only when the whole unit fits.
    selected: list[list[LLMMessage]] = [units[-1]]
    kept_tokens = _estimate_tokens(units[-1])
    for unit in reversed(units[:-1]):
        unit_tokens = _estimate_tokens(unit)
        if kept_tokens + unit_tokens > budget:
            continue
        selected.insert(0, unit)
        kept_tokens += unit_tokens
    kept = [item for unit in selected for item in unit]

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


@dataclass(frozen=True)
class _ReportingTrimResult:
    messages: list[LLMMessage]
    exhausted: bool = False
    reason: str | None = None


def _trim_reporting_context_to_budget(
    messages: list[LLMMessage],
    *,
    context_window: int = 128000,
    max_output: int = 4096,
) -> _ReportingTrimResult:
    """Trim a rebased reporting context without splitting typed units.

    The stable system prefix, typed task-state/capsule messages, current user
    tail and latest complete tool unit are mandatory.  If those mandatory
    units alone exceed the Provider budget, return a deterministic exhausted
    result instead of dropping a capsule or truncating prose mid-unit.
    """

    if not messages:
        return _ReportingTrimResult(messages=[])
    budget = max(0, int(context_window) - int(max_output) - _SAFETY_BUFFER)
    if _estimate_tokens(messages) <= budget:
        return _ReportingTrimResult(messages=messages)

    system = messages[0]
    rest = messages[1:]
    units = _atomic_history_units(rest)
    if not units:
        if _estimate_tokens([system]) > budget:
            return _ReportingTrimResult(
                messages=[system], exhausted=True, reason=CONTEXT_BUDGET_EXHAUSTED
            )
        return _ReportingTrimResult(messages=[system])

    mandatory_indices: set[int] = {len(units) - 1}
    for index, unit in enumerate(units):
        for message in unit:
            content = str(getattr(message, "content", "") or "")
            if any(
                marker in content
                for marker in (
                    "<typed_task_state>",
                    "<task_state_capsule>",
                    "<bounded_context>",
                    "<context_budget>",
                )
            ):
                mandatory_indices.add(index)
            if message is rest[-1] and message.role == "user" and not message.is_tool_result:
                mandatory_indices.add(index)

    mandatory_units = [units[index] for index in sorted(mandatory_indices)]
    mandatory = [system] + [item for unit in mandatory_units for item in unit]
    mandatory_tokens = _estimate_tokens(mandatory)
    if mandatory_tokens > budget:
        return _ReportingTrimResult(
            messages=mandatory,
            exhausted=True,
            reason=CONTEXT_BUDGET_EXHAUSTED,
        )

    selected_indices = set(mandatory_indices)
    used = mandatory_tokens
    for index in reversed(range(len(units))):
        if index in selected_indices:
            continue
        unit_tokens = _estimate_tokens(units[index])
        if used + unit_tokens > budget:
            continue
        selected_indices.add(index)
        used += unit_tokens

    selected = [item for index, unit in enumerate(units) if index in selected_indices for item in unit]
    result = [system] + selected
    return _ReportingTrimResult(messages=result)


def _compact_messages_for_working_memory(
    messages: list[LLMMessage],
    *,
    target_tokens: int = 36000,
) -> list[LLMMessage]:
    """Replace old working context with one structured model handoff.

    The complete in-process transcript is intentionally left untouched by this
    helper.  Only the Provider-facing working set is compacted.  Compaction is
    atomic around assistant tool calls/results and always retains the newest
    task boundary and protocol unit.  The handoff contains explicit progress,
    decisions, constraints, remaining work, and critical references extracted
    from structured messages (tool arguments/results and task XML), rather than
    taking a first-message slice or scraping identifiers with regular
    expressions.
    """

    if not messages or _estimate_tokens(messages) <= target_tokens:
        return messages

    system_msg = messages[0]
    history = [
        message
        for message in messages[1:]
        if _HANDOFF_SUMMARY_MARKER not in str(message.content or "")
    ]
    summary_payload = _build_handoff_summary(history)
    summary_content = _render_handoff_summary(summary_payload)
    handoff = LLMMessage(role="user", content=summary_content)

    fixed_tokens = _estimate_tokens([system_msg, handoff])
    recent_budget = max(0, int(target_tokens) - fixed_tokens)

    units = _atomic_history_units(history)
    kept: list[LLMMessage] = []
    kept_tokens = 0
    # Keep the newest unit no matter how large it is.  This is the active task
    # boundary/tool exchange and is required to finish an unfinished protocol.
    if units:
        newest = units[-1]
        kept = list(newest)
        kept_tokens = _estimate_tokens(newest)
        for unit in reversed(units[:-1]):
            unit_tokens = _estimate_tokens(unit)
            if kept_tokens + unit_tokens > recent_budget:
                continue
            kept[0:0] = unit
            kept_tokens += unit_tokens

    # A current user boundary can be a standalone unit after a very large tool
    # exchange.  Ensure the newest user task remains visible even if the latest
    # atomic unit is an assistant/tool pair.
    latest_user = next(
        (
            message
            for message in reversed(history)
            if message.role == "user" and not message.is_tool_result
        ),
        None,
    )
    if latest_user is not None and latest_user not in kept:
        user_tokens = _estimate_tokens([latest_user])
        if user_tokens <= recent_budget:
            kept.insert(0, latest_user)

    result = [system_msg, handoff, *kept]
    logger.info(
        "Working memory compacted: {} -> {} messages; handoff_sequence={}",
        len(messages),
        len(result),
        summary_payload.get("sequence", 1),
    )
    return result


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


def _json_object(content: Any) -> Mapping[str, Any] | None:
    """Decode one message as a JSON object without interpreting free prose."""

    try:
        payload = json.loads(str(content or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _bounded_text(value: Any, *, limit: int = 1200) -> str:
    """Normalize one summary field without selecting by a magic first slice."""

    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    # Keep both ends: conclusions often land at the end while the beginning
    # names the decision or task.  This is a bounded presentation field, not a
    # source of truth; the lossless transcript remains local.
    head = max(1, limit // 2)
    tail = max(1, limit - head - 32)
    return f"{text[:head]} … {text[-tail:]}"


def _structured_reference_values(value: Any, *, parent_key: str = "") -> list[str]:
    """Collect references from explicit structured fields only.

    This intentionally does not scan arbitrary prose for ``E-``/``Work/`` style
    ids.  References survive compaction when a tool/task payload identifies them
    under a ref/evidence/artifact/result field, avoiding regex-ID heuristics.
    """

    reference_keys = {
        "ref",
        "refs",
        "reference",
        "references",
        "artifact_ref",
        "artifact_refs",
        "evidence_id",
        "evidence_ids",
        "evidence_ref",
        "evidence_refs",
        "result_ref",
        "result_refs",
        "subject_ref",
        "input_ref",
        "input_refs",
        "prior_result_ref",
        "context_summary_ref",
        "context_summary_refs",
        "path",
        "paths",
    }
    key = str(parent_key or "").casefold()
    values: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            child_key = str(raw_key)
            if child_key.casefold() in reference_keys:
                values.extend(_flatten_reference_values(child))
            elif isinstance(child, (Mapping, list, tuple)):
                values.extend(
                    _structured_reference_values(child, parent_key=child_key)
                )
    elif isinstance(value, (list, tuple)) and key in reference_keys:
        for child in value:
            values.extend(_flatten_reference_values(child))
    return values


def _flatten_reference_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        output: list[str] = []
        for child in value:
            output.extend(_flatten_reference_values(child))
        return output
    return []


def _build_handoff_summary(history: Sequence[LLMMessage]) -> dict[str, Any]:
    """Build a deterministic Codex-style handoff payload from typed history."""

    progress: list[str] = []
    decisions: list[str] = []
    constraints: list[str] = []
    remaining: list[str] = []
    references: list[str] = []
    tool_state: list[dict[str, Any]] = []
    active_task: dict[str, str] = {}

    def append_unique(target: list[str], value: Any, *, limit: int = 12) -> None:
        rendered = _bounded_text(value)
        if rendered and rendered not in target and len(target) < limit:
            target.append(rendered)

    for message in history:
        content = str(message.content or "")
        if _HANDOFF_SUMMARY_MARKER in content:
            continue
        if message.role == "user" and not message.is_tool_result:
            # XML task boundaries are explicit structured state.  Keep the
            # current task identity/objective and constraints without copying
            # the entire contract into the handoff.
            if content.lstrip().startswith(("<task_context", "<task_boundary")):
                for field in ("task_id", "revision", "objective"):
                    open_tag = f"<{field}>"
                    close_tag = f"</{field}>"
                    if open_tag in content and close_tag in content:
                        value = content.split(open_tag, 1)[1].split(close_tag, 1)[0]
                        active_task[field] = _bounded_text(value, limit=400)
                for marker in ("<constraint>", "<allowed_output>", "<next_action>"):
                    if marker in content:
                        append_unique(
                            constraints if marker == "<constraint>" else remaining,
                            content.split(marker, 1)[1].split(marker.replace("<", "</", 1), 1)[0],
                        )
                append_unique(progress, active_task.get("objective") or content, limit=8)
            elif content.lstrip().startswith(("<semantic_turn", "<submission_correction", "<same_identity_continuation")):
                append_unique(remaining, content, limit=8)
            else:
                append_unique(progress, content, limit=8)
        elif message.role == "assistant" and content and not message.tool_calls:
            append_unique(progress, content, limit=12)
        if message.tool_calls:
            for call in message.tool_calls:
                arguments = dict(getattr(call, "arguments", {}) or {})
                references.extend(_structured_reference_values(arguments))
                tool_state.append(
                    {
                        "tool": str(getattr(call, "name", "") or ""),
                        "status": "requested",
                    }
                )
        if message.is_tool_result:
            payload = _json_object(message.content)
            if payload is not None:
                references.extend(_structured_reference_values(payload))
                state = {
                    key: payload[key]
                    for key in (
                        "status",
                        "accepted",
                        "complete",
                        "next_action",
                        "part_id",
                        "ready_part_ids",
                        "missing_part_ids",
                        "rewrite_part_ids",
                        "result_path",
                    )
                    if key in payload
                }
                if state:
                    state["tool_call_id"] = str(message.tool_call_id or "")
                    tool_state.append(state)
                    status = payload.get("status")
                    if status in {"failed", "blocked", "correction_required"}:
                        append_unique(remaining, payload.get("next_action") or status)
                    elif payload.get("accepted") is True or status in {"completed", "ok"}:
                        append_unique(decisions, payload.get("next_action") or status)
            else:
                append_unique(progress, message.content, limit=8)

    references = list(dict.fromkeys(item for item in references if item))[-64:]
    # The active unit is the final source of truth for unfinished work.  Keep a
    # concise tail of structured tool state rather than arbitrary old output.
    tool_state = tool_state[-24:]
    if not remaining:
        remaining.append("Continue the active task from the latest retained protocol unit.")
    return {
        "version": 1,
        "active_task": active_task,
        "progress": progress[-12:],
        "decisions": decisions[-12:],
        "constraints": constraints[-12:],
        "remaining_work": remaining[-12:],
        "critical_refs": references,
        "tool_state": tool_state,
        "sequence": 1,
    }


def _render_handoff_summary(payload: Mapping[str, Any]) -> str:
    """Render a compact XML envelope whose body is machine-readable JSON."""

    normalized = dict(payload)
    normalized["sequence"] = int(normalized.get("sequence", 1) or 1)
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"{_HANDOFF_SUMMARY_MARKER}\n{serialized}\n{_HANDOFF_SUMMARY_END}"


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
        provider_admission: Any | None = None,
        provider_admission_controller: Any | None = None,
        provider_admission_provider: str | None = None,
        provider_admission_model: str | None = None,
        provider_admission_identity_key: str | None = None,
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
        context_rebuilder: Any | None = None,
        pre_send_context_guard: Callable[..., Any] | None = None,
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
        # The durable professional identity owns one lossless conversation.
        # Reporting transitions update the active task metadata and append an
        # explicit boundary/delta message; they never clear this transcript.
        self._active_task_identity: dict[str, Any] | None = None
        self._task_boundaries: list[dict[str, Any]] = []
        self._handoff_summary: dict[str, Any] | None = None
        self._compaction_sequence = 0
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
        # Physical Provider admission is intentionally separate from typed
        # task/identity admission.  The lease acquired by the helpers below
        # wraps one actual request and is released before AgentLoop backoff.
        self.provider_admission = provider_admission or provider_admission_controller
        self.provider_admission_provider = provider_admission_provider
        self.provider_admission_model = provider_admission_model
        self.provider_admission_identity_key = provider_admission_identity_key
        self.provider_attempt_observer = provider_attempt_observer
        self.provider_attempt_record_observer = provider_attempt_record_observer
        # Optional typed context hook.  ``None`` intentionally preserves the
        # historical conversation-building path byte-for-byte.
        self.context_rebuilder = context_rebuilder
        # Optional synchronous/async policy hook.  The default gate remains
        # pure and fail-closed for reporting contexts; legacy loops with no
        # rebuilder retain their historical behavior.
        self.pre_send_context_guard = pre_send_context_guard
        self._last_provider_messages: list[LLMMessage] | None = None
        self._last_provider_tools: list[dict[str, Any]] | None = None
        self._last_provider_attempt_disposition: str | None = None
        self._last_provider_request_fingerprint: str | None = None
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
        """Backward-compatible no-op retained for older callers.

        A reporting identity is a continuous conversation.  Clearing history
        here used to make every typed task look like a brand-new Agent and is
        intentionally no longer supported.  Callers should use
        :meth:`begin_typed_task` to append a task boundary/delta.
        """

        if self._status not in {AgentStatus.IDLE, AgentStatus.ERROR}:
            raise RuntimeError("cannot change task context while the Agent is active")

    def begin_typed_task(self, context: Mapping[str, Any]) -> bool:
        """Record one task transition without resetting the identity transcript.

        ``context`` is persisted with the conversation trace and is used by
        the runner to decide whether the next prompt is a full task context or
        a delta.  The method returns ``True`` for the first task in a session.
        """

        if self._status not in {AgentStatus.IDLE, AgentStatus.ERROR}:
            raise RuntimeError("cannot change task context while the Agent is active")
        normalized = {
            str(key): value
            for key, value in dict(context or {}).items()
            if value is not None
        }
        task_id = str(normalized.get("task_id") or "")
        if not task_id:
            raise ValueError("typed task context requires task_id")
        first = self._active_task_identity is None
        previous = self._active_task_identity
        if previous is not None and previous == normalized:
            return False
        self._active_task_identity = normalized
        self._task_boundaries.append(
            {
                "previous": dict(previous) if previous is not None else None,
                "current": dict(normalized),
                "sequence": len(self._task_boundaries) + 1,
            }
        )
        # A new task may expose a different result-part namespace.  Keep the
        # conversation/tool protocol, but do not carry result-part write-cache
        # entries into the new task's local execution.
        self._persisted_result_part_contents = {}
        self._last_provider_messages = None
        self._last_provider_tools = None
        self._last_provider_attempt_disposition = None
        self._last_provider_request_fingerprint = None
        return first

    @property
    def active_task_identity(self) -> dict[str, Any] | None:
        return dict(self._active_task_identity) if self._active_task_identity else None

    @property
    def task_boundaries(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._task_boundaries]

    @property
    def handoff_summary(self) -> dict[str, Any] | None:
        return dict(self._handoff_summary) if self._handoff_summary else None

    def restore_conversation(
        self,
        messages: Sequence[Mapping[str, Any] | LLMMessage],
        *,
        task_boundaries: Sequence[Mapping[str, Any]] = (),
        handoff_summary: Mapping[str, Any] | None = None,
    ) -> None:
        """Restore a persisted lossless transcript after a process restart."""

        if self._status not in {AgentStatus.IDLE, AgentStatus.ERROR}:
            raise RuntimeError("cannot restore conversation while the Agent is active")

        restored: list[LLMMessage] = []
        for value in messages:
            if isinstance(value, LLMMessage):
                restored.append(value)
                continue
            if not isinstance(value, Mapping):
                continue
            calls = [
                LLMToolCall(
                    id=str(item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=dict(item.get("arguments") or {}),
                )
                for item in (value.get("tool_calls") or ())
                if isinstance(item, Mapping)
            ]
            restored.append(
                LLMMessage(
                    role=str(value.get("role") or "user"),
                    content=str(value.get("content") or ""),
                    tool_calls=calls or None,
                    tool_call_id=(
                        str(value.get("tool_call_id"))
                        if value.get("tool_call_id") is not None
                        else None
                    ),
                    is_tool_result=bool(value.get("is_tool_result", False)),
                    thinking=(
                        str(value.get("thinking"))
                        if value.get("thinking") is not None
                        else None
                    ),
                    cache_control=bool(value.get("cache_control", False)),
                )
            )
        self._conversation_history = restored
        self._task_boundaries = [dict(item) for item in task_boundaries if isinstance(item, Mapping)]
        if self._task_boundaries:
            current = self._task_boundaries[-1].get("current")
            self._active_task_identity = dict(current) if isinstance(current, Mapping) else None
        self._handoff_summary = (
            dict(handoff_summary) if isinstance(handoff_summary, Mapping) else None
        )
        self._compaction_sequence = int(
            self._handoff_summary.get("sequence", 0)
            if self._handoff_summary
            else 0
        )

    def set_context_rebuilder(self, rebuilder: Any | None) -> None:
        """Install or remove a typed Provider-context rebaser.

        The hook is deliberately opt-in so legacy AgentLoop users retain the
        existing conversation history semantics.  Rebuilders may be sync or
        async objects implementing ``rebuild`` (or a compatible callable).
        """

        self.context_rebuilder = rebuilder

    async def _mark_provider_context_delivered(
        self,
        messages: Sequence[LLMMessage],
        response: Any | None = None,
    ) -> None:
        """Commit a successful typed Provider payload as consumed context."""

        if self.context_rebuilder is None:
            return
        marker = getattr(
            self.context_rebuilder,
            "mark_provider_context_delivered",
            None,
        )
        if marker is None:
            return
        if "response" in inspect.signature(marker).parameters:
            result = marker(messages, response=response)
        else:
            # Compatibility with an earlier experimental hook that accepted
            # only the sent message list.
            result = marker(messages)
        if inspect.isawaitable(result):
            await result

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
            messages = await self._build_messages()
            if self.context_rebuilder is None:
                messages = await self._compact_working_memory_async(messages)
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

    async def _deliver_completed_report_terminal(self, message: UserMessage) -> bool:
        """Deterministically deliver a successful non-distillation report terminal."""

        if self.agent_id != "main":
            return False
        response = _canonical_completed_report_response(self.workspace, message)
        if response is None:
            return False
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

            if await self._deliver_completed_report_terminal(message):
                return

            # Build messages — system prompt always first, conversation history follows
            context_started = time.perf_counter()
            messages = await self._build_messages()

            # Legacy loops compact their local history here. Reporting loops
            # defer trimming until after the typed rebase in
            # ``_chat_with_retries`` so the current capsule/latest tool unit
            # cannot be dropped before the gate sees them.
            if self.context_rebuilder is None:
                messages = await self._compact_working_memory_async(messages)

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

    async def _acquire_provider_request_lease(
        self,
        messages: list[LLMMessage],
    ) -> Any | None:
        """Reserve one physical request, if the runner installed admission.

        The reservation is deliberately made after the pre-send context gate
        and immediately before invoking the adapter.  It therefore does not
        turn typed-task scheduling into a Provider cap and does not remain held
        while retry backoff sleeps.
        """

        controller = self.provider_admission
        if controller is None:
            return None
        provider = self.provider_admission_provider or getattr(
            self.llm_provider, "provider_type", self.llm_provider.__class__.__name__
        )
        model = self.provider_admission_model or getattr(
            self.llm_provider, "model", None
        )
        task_id = str(self.usage_task_id or getattr(self._current_message, "task_id", "") or "")
        identity_key = self.provider_admission_identity_key
        estimated_tokens = _estimate_tokens(messages) + max(
            0, int(getattr(self.config, "max_tokens", 0) or 0)
        )
        return await controller.acquire(
            provider,
            model,
            estimated_tokens=estimated_tokens,
            task_id=task_id or None,
            identity_key=identity_key,
        )

    async def _admitted_chat_stream(
        self,
        messages: list[LLMMessage],
        tool_definitions: list[dict] | None,
        stream_idle_timeout_seconds: float | None,
    ):
        """Yield one adapter stream while holding exactly one request lease."""

        lease = await self._acquire_provider_request_lease(messages)
        try:
            stream_kwargs: dict[str, Any] = {}
            if stream_idle_timeout_seconds is not None:
                stream_kwargs["stream_idle_timeout_seconds"] = stream_idle_timeout_seconds
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
                yield chunk
        except BaseException as exc:
            if lease is not None:
                await lease.release(error=exc)
                lease = None
            raise
        finally:
            if lease is not None:
                await lease.release()

    async def _admitted_chat(
        self,
        messages: list[LLMMessage],
        tool_definitions: list[dict] | None,
    ) -> Any:
        """Fallback non-streaming adapter call with its own request lease."""

        lease = await self._acquire_provider_request_lease(messages)
        try:
            return await self.llm_provider.chat(
                messages=messages,
                tools=tool_definitions,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        except BaseException as exc:
            if lease is not None:
                await lease.release(error=exc)
                lease = None
            raise
        finally:
            if lease is not None:
                await lease.release()

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
                stream = self._admitted_chat_stream(
                    messages,
                    tool_definitions,
                    stream_idle_timeout_seconds,
                )
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
                response = await self._admitted_chat(messages, tool_definitions)
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

    @staticmethod
    def _round_reason_for_phase(phase: str, attempt: int = 1) -> str:
        """Map an explicit loop phase to H1's canonical reason enum."""

        phase_text = str(phase or "initial")
        if int(attempt or 1) > 1:
            return "provider_retry"
        folded = phase_text.casefold()
        if folded == "tool_followup" or "evidence" in folded:
            return "evidence_lookup"
        if folded.startswith("guard"):
            return "tool_contract_error"
        if "continuation" in folded:
            return "long_output_continuation"
        if "correction" in folded or "revision" in folded:
            return "semantic_correction"
        return "direct_submit"

    def _gate_phase_for_message(self, phase: str) -> str:
        """Use the typed turn kind when a Runner starts a continuation turn.

        ReportingAgentRunner sends every ``one_turn`` through the same AgentLoop
        entry point, so the physical call's phase is ``initial`` even when the
        current UserMessage is a continuation/correction.  The turn kind is a
        durable semantic reason and is the only source used to widen the gate
        for that fresh logical round.
        """

        if str(phase or "initial").casefold() != "initial":
            return phase
        turn_kind = str(getattr(self._current_message, "turn_kind", "") or "")
        if turn_kind in {
            "submission_correction",
            "tool_slice_continuation",
            "max_tokens_continuation",
        }:
            return turn_kind
        return phase

    def _context_window_for_request(self) -> int:
        """Read an optional Provider/config context window without changing caps."""

        for owner in (self.config, self.llm_provider):
            for name in ("context_window", "max_context_tokens", "context_length"):
                value = getattr(owner, name, None)
                if value is not None:
                    try:
                        parsed = int(value)
                    except (TypeError, ValueError):
                        continue
                    if parsed > 0:
                        return parsed
        return 128_000

    def _apply_rebased_usage_metadata(self, rebased: Any) -> None:
        """Copy hash-only rebase metrics into the next ledger row."""

        manifest = getattr(rebased, "manifest", None)
        metrics = getattr(manifest, "metrics", None)
        if not isinstance(metrics, dict):
            metrics = {}
        for metric_name in (
            "new_chars",
            "repeated_chars",
            "stable_chars",
            "dynamic_chars",
            "repeated_stable_chars",
            "repeated_dynamic_chars",
            "duplicate_tool_result_chars",
            "duplicate_completed_result_chars",
            "duplicate_evidence_chars",
        ):
            setattr(self, f"usage_{metric_name}", int(metrics.get(metric_name, 0) or 0))
        if manifest is not None:
            version = getattr(manifest, "context_manifest_version", None)
            if version is None:
                version = getattr(manifest, "schema_version", None)
            if version is not None:
                self.usage_context_manifest_version = int(version)
            for attr, names in {
                "usage_context_manifest_ref": ("manifest_ref", "context_manifest_ref"),
                "usage_provider_call_ref": ("provider_call_ref", "provider_call_ref"),
                "usage_provider_call_hash": ("provider_call_hash", "provider_call_hash"),
            }.items():
                for name in names:
                    value = getattr(manifest, name, None)
                    if value:
                        setattr(self, attr, str(value))
                        break

    async def _invoke_pre_send_context_guard(
        self,
        messages: list[LLMMessage],
        tool_definitions: list[dict] | None,
        *,
        phase: str,
        attempt: int,
        rebuilt: bool,
        rebuild_count: int,
    ) -> ContextGateDecision:
        """Evaluate the optional policy hook and then the pure default gate."""

        gate_phase = self._gate_phase_for_message(phase)
        kwargs = {
            "messages": messages,
            "tool_definitions": tool_definitions,
            "phase": gate_phase,
            "attempt": attempt,
            "previous_messages": self._last_provider_messages,
            "previous_tool_definitions": self._last_provider_tools,
            "previous_attempt_disposition": self._last_provider_attempt_disposition,
            "rebuilt": rebuilt,
            "rebuild_count": rebuild_count,
            "loop": self,
        }
        decision: Any = None
        if self.pre_send_context_guard is not None:
            function = getattr(self.pre_send_context_guard, "check", self.pre_send_context_guard)
            try:
                decision = function(**kwargs)
            except TypeError:
                try:
                    decision = function(messages, tool_definitions, gate_phase, attempt)
                except TypeError:
                    decision = function(messages)
            if inspect.isawaitable(decision):
                decision = await decision
            if isinstance(decision, ContextGateDecision):
                return decision
            if isinstance(decision, dict):
                status = str(decision.get("status") or "allow")
                if status in {"allow", "rebuilt", "blocked"}:
                    return ContextGateDecision(
                        status,  # type: ignore[arg-type]
                        str(decision.get("reason") or "custom_guard"),
                        duplicate_tool_result_chars=int(decision.get("duplicate_tool_result_chars", 0) or 0),
                        duplicate_completed_result_chars=int(decision.get("duplicate_completed_result_chars", 0) or 0),
                        duplicate_evidence_chars=int(decision.get("duplicate_evidence_chars", 0) or 0),
                        rebuild_count=max(0, int(decision.get("rebuild_count", rebuild_count) or 0)),
                    )
            if isinstance(decision, str) and decision in {"allow", "rebuilt", "blocked"}:
                return ContextGateDecision(decision, "custom_guard", rebuild_count=rebuild_count)  # type: ignore[arg-type]

        # No implicit context policy is applied.  A tool error/result must be
        # returned to the same model turn so it can repair the local command;
        # it must not be converted into a pre-send block or a task restart.
        return ContextGateDecision("allow", "context_gate_disabled")

    def _record_pre_send_gate(
        self,
        decision: ContextGateDecision,
        *,
        phase: str,
        messages: Sequence[LLMMessage],
        tool_definitions: Sequence[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Persist a zero-cost pre-send gate event without calling Provider."""

        blocked = decision.status == "blocked"
        attempt_kind = "pre_send_block" if blocked else "pre_send_rebuild"
        reason = (
            "redundant_followup"
            if blocked and decision.reason.startswith("duplicate")
            else self._round_reason_for_phase(phase)
        )
        self.usage_provider_request_sent = False
        self.usage_attempt_kind = attempt_kind
        self.usage_round_reason = reason
        self.usage_pre_send_guard_status = decision.reason or decision.status
        self.usage_rebuild_count = max(0, int(decision.rebuild_count or 0))
        run_id = str(self.usage_run_id or self.agent_type)
        record = {
            "run_id": run_id,
            "task_id": str(self.usage_task_id or "") or None,
            "agent_id": str(self.agent_type),
            "stage": str(getattr(self, "usage_stage", "") or phase),
            "phase": phase,
            "status": "blocked" if blocked else "rebuilt",
            "error": decision.reason if blocked else None,
            "attempt": 0,
            "retry": False,
            "attempt_disposition": "not_sent",
            "retry_decision": "pre_send_block" if blocked else "pre_send_rebuild",
            "provider_request_sent": False,
            "attempt_kind": attempt_kind,
            "round_reason": reason,
            "reason_source": "pre_send_gate",
            "logical_round_id": getattr(self, "usage_logical_round_id", None),
            "parent_provider_call_id": getattr(self, "usage_parent_provider_call_id", None),
            # A pre-send event has no Provider payload manifest.  Do not point
            # it at the previous physical call's ref/hash.
            "context_manifest_ref": None,
            "provider_call_ref": None,
            "provider_call_hash": None,
            "context_manifest_version": getattr(self, "usage_context_manifest_version", None),
            "pre_send_guard_status": decision.reason or decision.status,
            "rebuild_count": max(0, int(decision.rebuild_count or 0)),
            "message_count": len(messages),
            "message_chars": sum(len(str(getattr(item, "content", "") or "")) for item in messages),
            "tool_schema_chars": len(json.dumps(tool_definitions or (), ensure_ascii=False, default=str)),
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "new_chars": 0,
            "repeated_chars": 0,
            "repeated_stable_chars": 0,
            "repeated_dynamic_chars": 0,
            "duplicate_tool_result_chars": decision.duplicate_tool_result_chars,
            "duplicate_completed_result_chars": decision.duplicate_completed_result_chars,
            "duplicate_evidence_chars": decision.duplicate_evidence_chars,
        }
        return UsageLedger(self.workspace, run_id).record_attempt(**record)

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
        rebuilt = False
        rebuild_count = 0
        if self.context_rebuilder is not None:
            # Rebase only the Provider-facing copy.  The lossless local
            # ``_conversation_history`` remains available to the forensic
            # trace and is never used as a fallback source by the rebaser.
            from .context_rebase import invoke_rebuilder

            rebased = await invoke_rebuilder(
                self.context_rebuilder,
                messages,
                tool_definitions,
                loop=self,
                phase=phase,
                user_message_id=user_message_id,
            )
            messages = rebased.messages
            if rebased.tool_definitions is not None:
                tool_definitions = rebased.tool_definitions
            rebuilt = True
            rebuild_count = 1
            self._apply_rebased_usage_metadata(rebased)

            # Reporting contexts are rebase-first.  Only after the typed view
            # exists may we remove older complete units; max_tokens and
            # max_tool_iterations remain untouched.
            trim = _trim_reporting_context_to_budget(
                list(messages),
                context_window=self._context_window_for_request(),
                max_output=self.config.max_tokens,
            )
            messages = trim.messages
            if trim.exhausted:
                decision = ContextGateDecision(
                    "blocked",
                    trim.reason or CONTEXT_BUDGET_EXHAUSTED,
                    rebuild_count=rebuild_count,
                )
                self._record_pre_send_gate(
                    decision,
                    phase=phase,
                    messages=messages,
                    tool_definitions=tool_definitions,
                )
                return _LoopLLMResponse(
                    content=CONTEXT_BUDGET_EXHAUSTED,
                    tool_calls=[],
                    stop_reason=CONTEXT_BUDGET_EXHAUSTED,
                )
        provider_messages = _sanitize_provider_messages(
            _provider_working_messages(messages)
        )
        provider_tool_definitions = _sanitize_provider_visible_value(
            tool_definitions
        )
        attempts = 0
        while True:
            attempts += 1
            decision = await self._invoke_pre_send_context_guard(
                provider_messages,
                provider_tool_definitions,
                phase=phase,
                attempt=attempts,
                rebuilt=rebuilt and attempts == 1,
                rebuild_count=rebuild_count if attempts == 1 else 0,
            )
            if decision.status == "blocked":
                self._record_pre_send_gate(
                    decision,
                    phase=phase,
                    messages=provider_messages,
                    tool_definitions=provider_tool_definitions,
                )
                return _LoopLLMResponse(
                    content=decision.reason or "pre_send_blocked",
                    tool_calls=[],
                    stop_reason="pre_send_blocked",
                )
            if decision.status == "rebuilt" and attempts == 1:
                self._record_pre_send_gate(
                    decision,
                    phase=phase,
                    messages=provider_messages,
                    tool_definitions=provider_tool_definitions,
                )
            # The next physical attempt must retain the exact typed payload;
            # only a proven no-output rejection may repeat it.
            self.usage_provider_request_sent = True
            self.usage_attempt_kind = "provider_request"
            self.usage_round_reason = self._round_reason_for_phase(phase, attempts)
            self.usage_pre_send_guard_status = None
            self.usage_rebuild_count = rebuild_count if attempts == 1 else 0
            self._last_provider_messages = list(provider_messages)
            self._last_provider_tools = list(provider_tool_definitions or ())
            self._last_provider_request_fingerprint = _context_payload_fingerprint(
                provider_messages,
                provider_tool_definitions,
            )
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
                # Only a successful physical request advances the typed context
                # delivery state.  Definite rejects keep the full payload for a
                # legal retry; accepted-or-unknown attempts never reach here and
                # remain fail-closed.
                await self._mark_provider_context_delivered(
                    provider_messages,
                    response,
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
                self._last_provider_attempt_disposition = "completed"
                await self._notify_provider_attempt_record(self._last_usage_record)
                return response
            except _ProviderAttemptError as failure:
                retry_number = attempts
                policy = classify_runtime_error(failure.original, retry_number=retry_number)
                attempt_disposition = failure.attempt_disposition
                self._last_provider_attempt_disposition = attempt_disposition.value
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
                    tool = self.tools.get(execution_tool_call.name)
                    if tool is None:
                        raise ValueError(f"Tool not found: {execution_tool_call.name}")

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
                        and terminal_payload.get("status") in {"completed", "delivered"}
                        and _report_workflow_operation(
                            self.workspace, terminal_payload
                        ) != "distill_template_skill"
                    ):
                        raise PermissionError(
                            "A successful report-workflow terminal turn is delivery-only. "
                            "Do not call files, status, run, resume, revise, cancel, or any "
                            "other tool; deliver the current run outputs and end the turn."
                        )
                    if (
                        self.agent_id == "main"
                        and terminal_payload is not None
                        and terminal_payload.get("status") in {"completed", "delivered"}
                        and _report_workflow_operation(
                            self.workspace, terminal_payload
                        ) != "distill_template_skill"
                    ):
                        raise PermissionError(
                            "A successful report-workflow terminal turn is delivery-only. "
                            "Do not call files, status, run, resume, revise, cancel, or any "
                            "other tool; deliver the current run outputs and end the turn."
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

                    required_args = _tool_required_args(
                        self.tools, execution_tool_call.name, tool
                    )
                    # submit_result exposes its complete business contract at the
                    # tool-argument root. Its validator owns partial-input feedback.
                    submit_result_owns_input_gate = (
                        execution_tool_call.name == "submit_result"
                    )

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
                        elif not submit_result_owns_input_gate:
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
                    if missing_required and not submit_result_owns_input_gate:
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
                            error=(
                                outcome.error
                                if outcome.status in {"failed", "blocked"}
                                else None
                            ),
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
                if self.context_rebuilder is None:
                    current_messages = await self._compact_working_memory_async(
                        current_messages
                    )
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
        """Compact a Provider working set and persist the handoff summary.

        The local ``_conversation_history`` remains lossless and is persisted by
        the reporting runner.  Only the structured summary is persisted here;
        no checkpoint reference or ``open_tool_result`` recovery token is ever
        placed in model-visible context.
        """

        compacted = _compact_messages_for_working_memory(
            messages,
            target_tokens=self.config.working_memory_tokens,
        )
        if compacted is messages:
            return messages
        summary_message = next(
            (
                item
                for item in compacted
                if _HANDOFF_SUMMARY_MARKER in str(item.content or "")
            ),
            None,
        )
        if summary_message is not None:
            try:
                raw = str(summary_message.content).split("\n", 1)[1]
                raw = raw.rsplit("\n", 1)[0]
                payload = json.loads(raw)
            except (IndexError, TypeError, ValueError, json.JSONDecodeError):
                payload = _build_handoff_summary(messages[1:])
            self._compaction_sequence += 1
            payload["sequence"] = self._compaction_sequence
            self._handoff_summary = payload
            # Keep the message's sequence in sync with the persisted payload.
            compacted[compacted.index(summary_message)] = LLMMessage(
                role="user",
                content=_render_handoff_summary(payload),
            )
            self._persist_handoff_summary(payload)
        logger.info(
            "Working memory compacted for {}: {} -> {} messages; handoff_sequence={}",
            self.agent_type,
            len(messages),
            len(compacted),
            self._compaction_sequence,
        )
        return compacted

    async def _request_model_handoff_summary(
        self,
        messages: Sequence[LLMMessage],
    ) -> dict[str, Any] | None:
        """Ask the configured Provider for a handoff summary at compaction.

        This is a normal Provider round (and therefore uses the same admission,
        retry, and usage ledger hooks as task work).  A failed/ambiguous summary
        call is never treated as a successful model compaction; callers retain a
        clearly marked deterministic fallback instead.
        """

        source = [
            message
            for message in messages
            if _HANDOFF_SUMMARY_MARKER not in str(message.content or "")
        ]
        # Preserve protocol units and keep the summarizer request within the
        # configured context window.  This is only the summarizer input; the
        # lossless local transcript remains untouched.
        summary_system = LLMMessage(
            role="system",
            content=(
                "You are the context-compaction handoff writer. Return one JSON object "
                "with exactly these top-level keys: progress, decisions, constraints, "
                "remaining_work, critical_refs. Each value is a short array of strings. "
                "Summarize only facts present in the transcript. Preserve exact refs "
                "from structured tool arguments/results; never invent ids or claim a "
                "tool was run when it was not. State the active task and unfinished "
                "tool protocol in remaining_work."
            ),
        )
        # Build the summarizer input from complete protocol units under the
        # same context budget used for ordinary requests.  The deterministic
        # state snapshot covers older evicted units; recent units remain exact.
        units = _atomic_history_units(source)
        selected_units: list[list[LLMMessage]] = []
        if units:
            selected_units = [units[-1]]
            selected_chars = sum(
                len(str(item.content or ""))
                for item in selected_units[0]
            )
            char_budget = max(
                4_096,
                int(
                    max(
                        4_096,
                        self._context_window_for_request()
                        - self.config.max_tokens
                        - _SAFETY_BUFFER,
                    )
                    / _TOKENS_PER_CHAR
                    * 0.5
                ),
            )
            for unit in reversed(units[:-1]):
                unit_chars = sum(len(str(item.content or "")) for item in unit)
                if selected_chars + unit_chars > char_budget:
                    continue
                selected_units.insert(0, unit)
                selected_chars += unit_chars
        selected_source = [
            item for unit in selected_units for item in unit
        ] or source[-8:]
        older_snapshot = _build_handoff_summary(source)
        older_snapshot_for_prompt = {
            key: (
                {
                    field: _bounded_text(field_value, limit=160)
                    for field, field_value in (
                        value.items() if isinstance(value, Mapping) else ()
                    )
                }
                if key == "active_task" and isinstance(value, Mapping)
                else [
                    _bounded_text(item, limit=160)
                    for item in (value or ())
                ]
                if isinstance(value, (list, tuple))
                else value
            )
            for key, value in older_snapshot.items()
            if key != "tool_state"
        }
        older_snapshot_for_prompt["tool_state"] = list(
            older_snapshot.get("tool_state") or ()
        )[-4:]
        transcript = []
        for message in selected_source:
            calls = [
                {
                    "id": str(getattr(call, "id", "") or ""),
                    "name": str(getattr(call, "name", "") or ""),
                    "arguments": dict(getattr(call, "arguments", {}) or {}),
                }
                for call in (getattr(message, "tool_calls", None) or ())
            ]
            transcript.append(
                {
                    "role": str(getattr(message, "role", "") or ""),
                    "content": str(getattr(message, "content", "") or ""),
                    "tool_call_id": getattr(message, "tool_call_id", None),
                    "is_tool_result": bool(getattr(message, "is_tool_result", False)),
                    "tool_calls": calls,
                }
            )
        serialized = json.dumps(
            transcript,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        summary_user = LLMMessage(
            role="user",
            content=(
                "<older_state_snapshot>\n"
                f"{json.dumps(older_snapshot_for_prompt, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n"
                "</older_state_snapshot>\n"
                "<compaction_transcript>\n"
                f"{serialized}\n"
                "</compaction_transcript>"
            ),
        )
        try:
            response = await self._chat_with_retries(
                [summary_system, summary_user],
                None,
                f"compaction-{self._compaction_sequence + 1}",
                phase="context_compaction",
                stream_idle_timeout_seconds=None,
            )
        except Exception as exc:
            logger.warning("Model handoff summary failed for {}: {}", self.agent_type, exc)
            return None
        content = str(getattr(response, "content", "") or "").strip()
        if not content:
            return None
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            # Providers occasionally wrap JSON in a short code fence.  Remove
            # only that syntactic wrapper; do not regex-scan or infer fields.
            if content.startswith("```") and content.endswith("```"):
                body = content.split("\n", 1)[1] if "\n" in content else ""
                body = body.rsplit("\n", 1)[0] if "\n" in body else body
                try:
                    parsed = json.loads(body)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return None
            else:
                return None
        if not isinstance(parsed, Mapping):
            return None
        required = (
            "progress",
            "decisions",
            "constraints",
            "remaining_work",
            "critical_refs",
        )
        if any(key not in parsed for key in required):
            return None
        normalized: dict[str, Any] = {
            key: [str(item) for item in (parsed.get(key) or ()) if str(item).strip()]
            if isinstance(parsed.get(key), (list, tuple))
            else [str(parsed.get(key))] if parsed.get(key) else []
            for key in required
        }
        normalized["version"] = 1
        normalized["source"] = "model"
        normalized["sequence"] = self._compaction_sequence + 1
        return normalized

    async def _compact_working_memory_async(
        self,
        messages: list[LLMMessage],
    ) -> list[LLMMessage]:
        """Compact with a model handoff, clearly marking deterministic fallback."""

        if not messages or _estimate_tokens(messages) <= self.config.working_memory_tokens:
            return messages
        baseline = _compact_messages_for_working_memory(
            messages,
            target_tokens=self.config.working_memory_tokens,
        )
        summary_message = next(
            (
                item
                for item in baseline
                if _HANDOFF_SUMMARY_MARKER in str(item.content or "")
            ),
            None,
        )
        if summary_message is None:
            return baseline
        model_summary = await self._request_model_handoff_summary(messages[1:])
        if model_summary is None:
            try:
                raw = str(summary_message.content).split("\n", 1)[1]
                raw = raw.rsplit("\n", 1)[0]
                fallback = json.loads(raw)
            except (IndexError, TypeError, ValueError, json.JSONDecodeError):
                fallback = _build_handoff_summary(messages[1:])
            fallback["source"] = "deterministic_fallback"
            fallback["fallback_reason"] = "model_handoff_unavailable"
            payload = fallback
        else:
            payload = model_summary
        self._compaction_sequence += 1
        payload["sequence"] = self._compaction_sequence
        self._handoff_summary = payload
        index = baseline.index(summary_message)
        baseline[index] = LLMMessage(role="user", content=_render_handoff_summary(payload))
        self._persist_handoff_summary(payload)
        return baseline

    def _persist_handoff_summary(self, payload: Mapping[str, Any]) -> None:
        """Persist one summary for crash/restart restoration when run-scoped."""

        run_id = str(self.usage_run_id or "").strip()
        if not run_id:
            return
        safe_agent = "".join(
            char if char.isalnum() or char in "-_." else "_"
            for char in str(self.agent_type)
        )
        path = self.workspace / f"Work/runs/{run_id}/agent-conversations/{safe_agent}.handoff.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(
            {
                "version": 1,
                "run_id": run_id,
                "agent_type": str(self.agent_type),
                "summary": dict(payload),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(serialized + "\n", encoding="utf-8")
        temporary.replace(path)

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
        round_reason = getattr(self, "usage_round_reason", None)
        if not round_reason:
            round_reason = self._round_reason_for_phase(phase, attempt)
        attempt_kind = str(
            getattr(self, "usage_attempt_kind", None) or "provider_request"
        )
        provider_request_sent = bool(
            getattr(self, "usage_provider_request_sent", True)
        )
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
            "provider_call_ref": getattr(self, "usage_provider_call_ref", None)
            or context_manifest_ref,
            "provider_call_hash": getattr(self, "usage_provider_call_hash", None),
            "provider_request_sent": provider_request_sent,
            "attempt_kind": attempt_kind,
            "round_reason": round_reason,
            "reason_source": getattr(self, "usage_reason_source", None)
            or "agent_loop",
            "logical_round_id": getattr(self, "usage_logical_round_id", None),
            "parent_provider_call_id": getattr(
                self, "usage_parent_provider_call_id", None
            ),
            "context_manifest_version": getattr(
                self, "usage_context_manifest_version", None
            ),
            "pre_send_guard_status": getattr(
                self, "usage_pre_send_guard_status", None
            ),
            "rebuild_count": int(
                getattr(self, "usage_rebuild_count", 0) or 0
            ),
            "new_chars": int(getattr(self, "usage_new_chars", 0) or 0),
            "repeated_chars": int(
                getattr(self, "usage_repeated_chars", 0) or 0
            ),
            "repeated_stable_chars": int(
                getattr(self, "usage_repeated_stable_chars", 0) or 0
            ),
            "repeated_dynamic_chars": int(
                getattr(self, "usage_repeated_dynamic_chars", 0) or 0
            ),
            "duplicate_tool_result_chars": int(
                getattr(self, "usage_duplicate_tool_result_chars", 0) or 0
            ),
            "duplicate_completed_result_chars": int(
                getattr(self, "usage_duplicate_completed_result_chars", 0) or 0
            ),
            "duplicate_evidence_chars": int(
                getattr(self, "usage_duplicate_evidence_chars", 0) or 0
            ),
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
        if self._handoff_summary and not any(
            _HANDOFF_SUMMARY_MARKER in str(message.content or "")
            for message in sanitized_history
        ):
            sanitized_history.insert(
                0,
                LLMMessage(
                    role="user",
                    content=_render_handoff_summary(self._handoff_summary),
                ),
            )
        # This method only assembles the lossless local baseline.  The typed
        # Provider rebaser is invoked exactly once by ``_chat_with_retries``
        # for each logical round, after tool schemas are available and before
        # any budget trim or network admission.
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
