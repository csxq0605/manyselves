"""Classify runtime failures for bounded, user-visible retry handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any


@dataclass(frozen=True)
class RuntimeErrorPolicy:
    """How one runtime error should be presented and whether it may be retried."""

    category: str
    title: str
    retryable: bool
    delay_seconds: float


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _response_headers(exc: BaseException) -> Any:
    response = getattr(exc, "response", None)
    return getattr(response, "headers", None)


def _retry_after_seconds(exc: BaseException) -> float | None:
    headers = _response_headers(exc)
    if headers is None:
        return None
    value = headers.get("retry-after") if hasattr(headers, "get") else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(value))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, min(seconds, 120.0))


def classify_runtime_error(exc: BaseException, retry_number: int = 1) -> RuntimeErrorPolicy:
    """Return a conservative retry policy for provider and runtime errors.

    ``retry_number`` is one-based and describes the retry that would happen
    next. Unknown, authentication, request, and validation failures are never
    retried automatically.
    """

    retry_number = max(1, retry_number)
    code = _status_code(exc)
    name = type(exc).__name__.casefold()
    message = str(exc).casefold()

    if isinstance(exc, asyncio.CancelledError):
        return RuntimeErrorPolicy("cancelled", "操作已中断", False, 0.0)

    if code == 429 or "rate limit" in message or "请求数限制" in message:
        retry_after = _retry_after_seconds(exc)
        delay = retry_after if retry_after is not None else (5.0 if retry_number == 1 else 15.0)
        return RuntimeErrorPolicy("rate_limit", "请求频率受限（429）", True, delay)

    if code in {408, 425} or isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in name:
        return RuntimeErrorPolicy(
            "timeout",
            "请求超时",
            True,
            1.0 if retry_number == 1 else 3.0,
        )

    if (
        "connection" in name
        or "connect" in message
        or "connection reset" in message
        or "network" in message
    ):
        return RuntimeErrorPolicy(
            "connection",
            "网络连接失败",
            True,
            1.0 if retry_number == 1 else 3.0,
        )

    if code == 409:
        return RuntimeErrorPolicy(
            "conflict",
            "服务端状态冲突（409）",
            True,
            1.0 if retry_number == 1 else 3.0,
        )

    if code is not None and 500 <= code <= 599:
        return RuntimeErrorPolicy(
            "server",
            f"服务端错误（{code}）",
            True,
            2.0 if retry_number == 1 else 5.0,
        )

    if code in {401, 403} or "authentication" in name or "permission" in name:
        return RuntimeErrorPolicy("authentication", "API鉴权失败", False, 0.0)

    if code in {400, 404, 405, 413, 415, 422}:
        return RuntimeErrorPolicy("invalid_request", f"请求不可用（{code}）", False, 0.0)

    if isinstance(exc, (ValueError, TypeError)):
        return RuntimeErrorPolicy("validation", "数据或参数校验失败", False, 0.0)

    return RuntimeErrorPolicy("unknown", "运行时错误", False, 0.0)


def runtime_error_details(exc: BaseException) -> dict[str, Any]:
    """Structured details suitable for the GUI/backend Error message."""

    policy = classify_runtime_error(exc)
    return {
        "category": policy.category,
        "retryable": policy.retryable,
        "status_code": _status_code(exc),
    }
