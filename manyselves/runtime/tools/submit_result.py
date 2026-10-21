"""Generic typed-result submission tool for one Agent task.

The tool validates the submission against the task's output contract.  A valid
submission is persisted and announced as the task's typed terminal.  An invalid
one is reported as a recovery event and answered with correction feedback in
the same conversation; a repeated defect or an exhausted budget ends the task
with a persisted failure instead of guessing content.

An Agent that produces several contracts gets the generic form instead: the
tool declares only "an object", the same for every task, and the task message
carries the task's own schema.  The tool list of a conversation then stays the
same from task to task, so the provider's prompt cache keeps hitting, while the
model still fills one flat object exactly as that schema says.  The result is
validated against the task's contract on receipt, as in the plain form.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from manyselves.interfaces.types import AgentResultMessage
from manyselves.kernel.contracts import ContractAdapter, ContractValidationError
from manyselves.kernel.recovery import RecoveryActionKind

from ..agent_results import FileAgentResultStore
from .registry import Tool

RecoveryCallback = Callable[[str, dict[str, Any]], Awaitable[Any]]


#: The generic form's arguments: one object, whatever the task's contract.
GENERIC_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}


class SubmitResultTool(Tool):
    name = "submit_result"
    side_effect = "terminal"
    parallel_safe = False
    max_validation_failures = 8

    def __init__(
        self,
        *,
        sender: str,
        workflow_id: str,
        run_id: str,
        task_id: str,
        task_attempt_id: str,
        session_id: str,
        conversation_id: str,
        agent_id: str,
        input_value: Any,
        output_contract_id: str,
        output_contract: ContractAdapter,
        store: FileAgentResultStore,
        bus: Any,
        recovery_callback: RecoveryCallback | None = None,
        generic: bool = False,
        store_task_id: str | None = None,
    ) -> None:
        self.sender = sender
        self.workflow_id = workflow_id
        self.run_id = run_id
        self.task_id = task_id
        self.task_attempt_id = task_attempt_id
        self.session_id = session_id
        self.conversation_id = conversation_id
        self.agent_id = agent_id
        self.input_value = input_value
        self.output_contract_id = output_contract_id
        self.output_contract = output_contract
        self.store = store
        self.bus = bus
        self.recovery_callback = recovery_callback
        self.store_task_id = store_task_id or task_id
        self.generic = generic
        if output_contract.json_schema().get("type") != "object":
            raise ValueError(
                f"submit_result requires an object output contract: {output_contract_id}"
            )
        if generic:
            self.input_schema = dict(GENERIC_SCHEMA)
            self.description = (
                "提交当前任务的最终结构化结果。参数直接按任务消息里 output_schema "
                "给出的 schema 填写，不要包一层，也不要传 JSON 字符串。"
            )
        else:
            self.input_schema = output_contract.json_schema()
            self.description = (
                f"提交本任务的最终结构化结果（{output_contract_id}）。"
                "参数直接按 schema 填写，不要包一层 payload，也不要传 JSON 字符串。"
            )
        self._failures = 0
        self._seen_errors: dict[str, int] = {}

    async def __call__(self, **submission: Any) -> dict[str, Any]:
        try:
            result = self.output_contract.validate(dict(submission))
        except ContractValidationError as exc:
            return await self._correction(str(exc))
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        result_ref = self.store.save(
            conversation_id=self.conversation_id,
            task_id=self.store_task_id,
            agent_id=self.agent_id,
            input_value=self.input_value,
            status="completed",
            result=result,
        )
        await self._publish(result_ref, "completed", "")
        return {"status": "ok", "accepted": True, "result_path": result_ref}

    async def _correction(self, error: str) -> dict[str, Any]:
        self._failures += 1
        repeated = self._seen_errors.get(error, 0) + 1
        self._seen_errors[error] = repeated
        decision = None
        if self.recovery_callback is not None:
            decision = await self.recovery_callback(
                "invalid_structured_output",
                {"task_id": self.task_id, "error": error},
            )
        declared_action = getattr(decision, "action", None)
        stop = (
            repeated >= 2
            or self._failures >= self.max_validation_failures
            or declared_action in {RecoveryActionKind.STOP, RecoveryActionKind.FAIL}
        )
        if not stop:
            feedback: dict[str, Any] = {
                "status": "correction_required",
                "accepted": False,
                "error": error,
                "output_contract": self.output_contract_id,
                "remaining_attempts": self.max_validation_failures - self._failures,
                "instruction": (
                    "结果不符合 output contract。只按 error 修正对应字段，"
                    "再调用一次 submit_result 提交完整对象；不要重复同一个被拒的提交。"
                ),
            }
            prompt = getattr(decision, "prompt", None)
            if prompt:
                feedback["instruction"] = prompt
            return feedback
        if repeated >= 2:
            cause = "the same validation error was repeated"
        elif self._failures >= self.max_validation_failures:
            cause = "the correction budget was exhausted"
        else:
            cause = "the recovery policy stopped correction"
        reason = f"structured submission failed: {cause}; error={error}"
        result_ref = self.store.save(
            conversation_id=self.conversation_id,
            task_id=self.store_task_id,
            agent_id=self.agent_id,
            input_value=self.input_value,
            status="failed",
            reason=reason,
        )
        await self._publish(result_ref, "failed", reason)
        return {
            "status": "failed",
            "accepted": False,
            "error": reason,
            "result_path": result_ref,
            "instruction": "不要再提交；本任务已按失败结束。",
        }

    async def _publish(self, result_ref: str, status: str, content: str) -> None:
        await self.bus.publish(
            AgentResultMessage(
                workflow_id=self.workflow_id,
                task_id=self.task_id,
                run_id=self.run_id,
                sender=self.sender,
                recipient="workflow",
                artifact_refs=[result_ref],
                result_path=result_ref,
                status=status,
                content=content,
                task_attempt_id=self.task_attempt_id,
                session_id=self.session_id,
            )
        )


__all__ = ["GENERIC_SCHEMA", "SubmitResultTool"]
