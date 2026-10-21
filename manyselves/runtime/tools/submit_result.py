"""Generic typed-result submission tool for one Agent task.

The tool validates the submission against the task's output contract.  A valid
submission is persisted and announced as the task's typed terminal.  An invalid
one is reported as a recovery event and answered with correction feedback in
the same conversation; a repeated defect or an exhausted budget ends the task
with a persisted failure instead of guessing content.

An Agent that produces several contracts can be given all of them.  The tool
then shows one stable schema with one property per contract, so the tool list
of a conversation stays the same from task to task and the provider's prompt
cache keeps hitting; each task fills only the property of its own contract.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from manyselves.interfaces.types import AgentResultMessage
from manyselves.kernel.contracts import ContractAdapter, ContractValidationError
from manyselves.kernel.recovery import RecoveryActionKind

from ..agent_results import FileAgentResultStore
from .registry import Tool

RecoveryCallback = Callable[[str, dict[str, Any]], Awaitable[Any]]


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
        all_contracts: Mapping[str, ContractAdapter] | None = None,
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
        self.keyed = bool(
            all_contracts
            and len(all_contracts) > 1
            and output_contract_id in all_contracts
        )
        if self.keyed:
            assert all_contracts is not None
            self.input_schema = keyed_schema(all_contracts)
            self.description = (
                "提交当前任务的最终结构化结果。参数只填当前任务指定的那一个"
                "结果名，值按该结果的 schema 填写；不要填别的结果名，也不要传 JSON 字符串。"
            )
        else:
            self.input_schema = output_contract.json_schema()
            if self.input_schema.get("type") != "object":
                raise ValueError(
                    f"submit_result requires an object output contract: {output_contract_id}"
                )
            self.description = (
                f"提交本任务的最终结构化结果（{output_contract_id}）。"
                "参数直接按 schema 填写，不要包一层 payload，也不要传 JSON 字符串。"
            )
        self._failures = 0
        self._seen_errors: dict[str, int] = {}

    async def __call__(self, **submission: Any) -> dict[str, Any]:
        if self.keyed:
            others = sorted(key for key in submission if key != self.output_contract_id)
            if others or self.output_contract_id not in submission:
                return await self._correction(
                    f"本任务只提交 {self.output_contract_id} 这一项"
                    + (f"，不要填 {'、'.join(others)}" if others else "")
                    + "。"
                )
            submission = submission[self.output_contract_id]
            if not isinstance(submission, Mapping):
                return await self._correction(
                    f"{self.output_contract_id} 的值必须是对象，不是字符串或列表。"
                )
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


def keyed_schema(contracts: Mapping[str, ContractAdapter]) -> dict[str, Any]:
    """One object schema with one optional property per contract id.

    Each contract's ``$defs`` move to the root under ``<contract id>__<name>``
    so that definitions of different contracts never collide.
    """
    properties: dict[str, Any] = {}
    definitions: dict[str, Any] = {}
    for contract_id in sorted(contracts):
        schema = contracts[contract_id].json_schema()
        if schema.get("type") != "object":
            raise ValueError(
                f"submit_result requires an object output contract: {contract_id}"
            )
        prefix = f"{contract_id}__"
        schema = _prefixed_refs(dict(schema), prefix)
        for name, definition in (schema.pop("$defs", None) or {}).items():
            definitions[prefix + name] = definition
        properties[contract_id] = schema
    combined: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        "minProperties": 1,
        "maxProperties": 1,
    }
    if definitions:
        combined["$defs"] = definitions
    return combined


def _prefixed_refs(value: Any, prefix: str) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str) and item.startswith("#/$defs/"):
                out[key] = "#/$defs/" + prefix + item[len("#/$defs/"):]
            elif key == "$defs" and isinstance(item, dict):
                out[key] = {name: _prefixed_refs(d, prefix) for name, d in item.items()}
            else:
                out[key] = _prefixed_refs(item, prefix)
        return out
    if isinstance(value, list):
        return [_prefixed_refs(item, prefix) for item in value]
    return value


__all__ = ["SubmitResultTool", "keyed_schema"]
