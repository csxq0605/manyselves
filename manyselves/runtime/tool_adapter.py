"""Adapters from declarative ToolDefinition to the current Tool runtime."""

from collections.abc import Callable, Mapping
from inspect import isawaitable
from typing import Any, cast

from pydantic import BaseModel

from manyselves.kernel.contracts import ContractAdapter
from manyselves.kernel.definitions import ToolDefinition
from manyselves.kernel.ports import ToolInvocationOutcome
from manyselves.runtime.tools.outcomes import normalize_tool_outcome
from manyselves.runtime.tools.registry import Tool
from manyselves.runtime.tools.result_memory import RunToolResultIndex


class ToolAdapterError(ValueError):
    """Raised when a ToolDefinition cannot bind to the current runtime."""


class ToolAdapter:
    """Shared contract validation and completed-result reuse for Tool adapters."""

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


class CapabilityToolAdapter(ToolAdapter):
    """Adapt one callable owned by a file-defined Capability."""

    async def invoke(
        self,
        arguments: Any,
        *,
        task_id: str,
    ) -> ToolInvocationOutcome:
        """Invoke a Capability with the value validated by its input contract."""

        validated_input = self.input_contract.validate(arguments)
        cache_arguments = _capability_cache_arguments(validated_input)
        reused = self._reused_outcome(task_id, cache_arguments)
        if reused is not None:
            return reused

        raw_result = cast(_ResolvedCapabilityTool, self.tool).invoke(validated_input)
        if isawaitable(raw_result):
            raw_result = await raw_result
        capability_outcome = normalize_tool_outcome(raw_result, self.tool.name)
        validated_result = capability_outcome.result
        if capability_outcome.status == "ok":
            validated_result = self.output_contract.validate(validated_result)
        elif self.error_contract is not None:
            validated_result = self.error_contract.validate(validated_result)
        outcome = ToolInvocationOutcome(
            status=capability_outcome.status,
            result=validated_result,
            error=capability_outcome.error,
            artifact_refs=capability_outcome.artifact_refs,
        )
        if (
            outcome.status == "ok"
            and self.reuse_result
            and self.result_index is not None
        ):
            self.result_index.record(
                task_id,
                self.definition.id,
                cache_arguments,
                outcome.model_dump(mode="json"),
            )
        return outcome


_CapabilityImplementationResolver = (
    Mapping[str, Callable[[Any], Any]]
    | Callable[[str], Callable[[Any], Any] | None]
)


class _ResolvedCapabilityTool(Tool):
    def __init__(
        self,
        name: str,
        implementation: Callable[[Any], Any],
    ) -> None:
        self.name = name
        self._implementation = implementation

    def __call__(self, **kwargs: Any) -> Any:
        return self._implementation(dict(kwargs))

    def invoke(self, arguments: Any) -> Any:
        return self._implementation(arguments)


class CapabilityToolAdapterFactory:
    """Build Tool adapters from ``capability:<id>:<implementation>`` refs."""

    def __init__(
        self,
        capability_id: str,
        implementations: _CapabilityImplementationResolver,
        contracts: Mapping[str, ContractAdapter],
        result_index: RunToolResultIndex | None = None,
    ) -> None:
        self._capability_id = capability_id
        self._implementations = implementations
        self._contracts = contracts
        self._result_index = result_index

    def build(self, definition: ToolDefinition) -> CapabilityToolAdapter:
        implementation_id = _capability_implementation_id(
            definition.implementation,
            self._capability_id,
        )
        implementation = _resolve_capability_implementation(
            self._implementations,
            implementation_id,
        )
        if implementation is None or not callable(implementation):
            raise ToolAdapterError(
                "capability Tool implementation is not registered: "
                f"{self._capability_id}:{implementation_id}"
            )
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
        return CapabilityToolAdapter(
            definition,
            _ResolvedCapabilityTool(definition.id, implementation),
            input_contract,
            output_contract,
            error_contract,
            self._result_index,
        )


def _capability_implementation_id(reference: str, capability_id: str) -> str:
    prefix, separator, remainder = reference.partition(":")
    if prefix != "capability" or not separator:
        raise ToolAdapterError(f"unsupported capability Tool reference: {reference}")
    owner, separator, implementation_id = remainder.partition(":")
    if owner != capability_id:
        raise ToolAdapterError(
            "capability Tool reference belongs to "
            f"{owner or '<empty>'}, expected {capability_id}"
        )
    if not separator or not implementation_id:
        raise ToolAdapterError(f"invalid capability Tool reference: {reference}")
    return implementation_id


def _resolve_capability_implementation(
    implementations: _CapabilityImplementationResolver,
    implementation_id: str,
) -> Callable[[Any], Any] | None:
    if isinstance(implementations, Mapping):
        return implementations.get(implementation_id)
    return implementations(implementation_id)


def _capability_cache_arguments(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return dict(value)
    return value
