"""Watch Runs stop; resume temporary failures; tell Main the outcome.

When an execution stops, the supervisor reads where and why:

- a ``temporary`` or ``model`` failure is resumed automatically, at most
  ``max_auto_resumes`` times in a row for one Run.  Resume continues the
  same Run id; completed steps are not redone;
- any other stop (completed, waiting for input, a program failure, or a
  failure that is still there after the automatic resumes) is sent to Main
  as a system message, so Main can explain it and decide with the user.

It knows no Capability; the failure classes come from ``run_failure``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from loguru import logger

from manyselves.runtime.run_failure import RunFailure

Notify = Callable[[str], Awaitable[None]]

_CATEGORY_TITLES = {
    "temporary": "模型服务或网络临时故障",
    "model": "模型没有给出可用结果",
    "program": "程序步骤出错",
    "cancelled": "运行被停止",
}


class RunSupervisor:
    """Auto-resume bounded temporary failures and report every final stop."""

    def __init__(
        self,
        projection: Any,
        notify: Notify | None,
        *,
        max_auto_resumes: int = 2,
        resume_delay_seconds: float = 5.0,
    ) -> None:
        self._projection = projection
        self._notify = notify
        self.max_auto_resumes = max_auto_resumes
        self.resume_delay_seconds = resume_delay_seconds
        self._auto_resumes: dict[str, int] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def auto_resumes(self, run_id: str) -> int:
        return self._auto_resumes.get(run_id, 0)

    def reset(self, run_id: str) -> None:
        """A person resumed or revised the Run: count automatic tries afresh."""

        self._auto_resumes.pop(run_id, None)

    async def on_settled(self, run_id: str) -> None:
        try:
            run = self._projection.get_run(run_id)["run"]
        except LookupError:
            return
        status = str(run.get("status"))
        if status == "failed":
            failure = self._projection.describe_failure(run_id)
            used = self._auto_resumes.get(run_id, 0)
            if failure is not None and failure.retryable and used < self.max_auto_resumes:
                self._auto_resumes[run_id] = used + 1
                self._spawn(self._resume_later(run_id))
                return
            await self._send(_failure_notice(run_id, run, failure, used))
            return
        if status == "waiting":
            await self._send(_notice(run_id, run, "等待输入", "运行在等待确认或补充输入。"))
            return
        if status == "completed":
            used = self._auto_resumes.pop(run_id, 0)
            extra = f"其间自动续跑 {used} 次。" if used else ""
            await self._send(_notice(run_id, run, "已完成", "运行已完成。" + extra))

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _resume_later(self, run_id: str) -> None:
        await asyncio.sleep(self.resume_delay_seconds)
        logger.info(
            "auto-resuming run {} ({}/{})",
            run_id,
            self._auto_resumes.get(run_id, 0),
            self.max_auto_resumes,
        )
        try:
            await self._projection.resume(uuid4(), run_id, automatic=True)
        except Exception as exc:  # noqa: BLE001 - reported to Main instead
            await self._send(
                _notice(run_id, {}, "自动续跑失败", f"自动续跑没有启动：{exc}")
            )

    async def _send(self, text: str) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(text)
        except Exception:  # noqa: BLE001 - a notice never fails the Run
            logger.exception("could not notify Main about a run")

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _notice(run_id: str, run: dict[str, Any], status: str, body: str) -> str:
    workflow = run.get("workflow_id") or ""
    return (
        f'<run_notice run_id="{run_id}" workflow="{workflow}" status="{status}">\n'
        f"{body}\n"
        "</run_notice>"
    )


def _failure_notice(
    run_id: str, run: dict[str, Any], failure: RunFailure | None, used: int
) -> str:
    if failure is None:
        return _notice(run_id, run, "失败", "运行失败，没有记录原因。")
    lines = [
        f"类型：{_CATEGORY_TITLES.get(failure.category, failure.category)}"
        f"（{failure.category}）。",
        "位置：" + (" / ".join(failure.path) or "未知") + "。",
        "错误：" + failure.error[:1200],
    ]
    if used:
        lines.append(f"已自动续跑 {used} 次，仍然失败，不再自动续跑。")
    if failure.category == "program":
        lines.append("程序错误续跑会以同样方式失败，需要先修代码或输入。")
    else:
        lines.append(
            "可以用 resume_run 续跑：沿用同一运行编号，已完成的步骤和模型回合不会重做。"
        )
    return _notice(run_id, run, "失败", "\n".join(lines))


__all__ = ["RunSupervisor"]
