"""Adapters from declarative ToolDefinition to the current Tool runtime."""

from collections.abc import Mapping
from inspect import isawaitable
from typing import Any

from pydantic import BaseModel

from manyselves.core.tools.outcomes import normalize_tool_outcome
from manyselves.core.tools.registry import Tool, ToolRegistry
from manyselves.core.tools.result_memory import RunToolResultIndex
from manyselves.kernel.contracts import ContractAdapter
from manyselves.kernel.definitions import ToolDefinition
from manyselves.kernel.ports import ToolInvocationOutcome


class ToolAdapterError(ValueError):
    """Raised when a ToolDefinition cannot bind to the current runtime."""


class LegacyToolAdapter:
    def __init__(
        self,
        definition: ToolDefinition,
        tool: Tool,
        input_contract: ContractAdapter,
        output_contract: ContractAdapter,
        error_contract: ContractAdapter | None,
        result_index: RunToolResultIndex | None,
    ) -> None:
        self.definition = definition
        self.tool = tool
        self.input_contract = input_contract
        self.output_contract = output_contract
        self.error_contract = error_contract
        self.result_index = result_index
        self.side_effect = definition.side_effect
        self.parallel_safe = definition.parallel_safe
        self.reuse_result = definition.reuse_result

    async def invoke(
        self,
        arguments: Any,
        *,
        task_id: str,
    ) -> ToolInvocationOutcome:
        validated_input = self.input_contract.validate(arguments)
        if isinstance(validated_input, BaseModel):
            validated_input = validated_input.model_dump(mode="python")
        if not isinstance(validated_input, Mapping):
            raise ToolAdapterError(
                f"tool input contract must produce an object: {self.definition.id}"
            )
        keyword_arguments = dict(validated_input)
        reused = self._reused_outcome(task_id, keyword_arguments)
        if reused is not None:
            return reused

        raw_result = self.tool(**keyword_arguments)
        if isawaitable(raw_result):
            raw_result = await raw_result
        legacy_outcome = normalize_tool_outcome(raw_result, self.tool.name)
        validated_result = legacy_outcome.result
        if legacy_outcome.status == "ok":
            validated_result = self.output_contract.validate(validated_result)
        elif self.error_contract is not None:
            validated_result = self.error_contract.validate(validated_result)
        outcome = ToolInvocationOutcome(
            status=legacy_outcome.status,
            result=validated_result,
            error=legacy_outcome.error,
            artifact_refs=legacy_outcome.artifact_refs,
        )
        if (
            outcome.status == "ok"
            and self.reuse_result
            and self.result_index is not None
        ):
            self.result_index.record(
                task_id,
                self.definition.id,
                keyword_arguments,
                outcome.model_dump(mode="json"),
            )
        return outcome

    def _reused_outcome(
        self,
        task_id: str,
        arguments: dict[str, Any],
    ) -> ToolInvocationOutcome | None:
        if not self.reuse_result or self.result_index is None:
            return None
        entry = self.result_index.lookup(task_id, self.definition.id, arguments)
        if entry is None or entry.get("status") != "completed":
            return None
        return ToolInvocationOutcome.model_validate(entry.get("result")).model_copy(
            update={"reused": True}
        )


class LegacyToolAdapterFactory:
    def __init__(
        self,
        tools: ToolRegistry,
        contracts: Mapping[str, ContractAdapter],
        result_index: RunToolResultIndex | None = None,
    ) -> None:
        self._tools = tools
        self._contracts = contracts
        self._result_index = result_index

    def build(self, definition: ToolDefinition) -> LegacyToolAdapter:
        prefix, separator, tool_name = definition.implementation.partition(":")
        if prefix != "legacy" or not separator or not tool_name:
            raise ToolAdapterError(
                f"unsupported current Tool reference: {definition.implementation}"
            )
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ToolAdapterError(f"current Tool is not registered: {tool_name}")
        try:
            input_contract = self._contracts[definition.input_contract]
            output_contract = self._contracts[definition.output_contract]
            error_contract = (
                self._contracts[definition.error_contract]
                if definition.error_contract
                else None
            )
        except KeyError as exc:
            raise ToolAdapterError(f"contract adapter is missing: {exc.args[0]}") from exc
        return LegacyToolAdapter(
            definition,
            tool,
            input_contract,
            output_contract,
            error_contract,
            self._result_index,
        )
