"""Expose a file-defined Capability Tool to a model as an AgentLoop Tool."""

from __future__ import annotations

from typing import Any

from manyselves.kernel.contracts import ContractValidationError
from manyselves.kernel.definitions import ToolDefinition

from .registry import Tool


class DefinitionTool(Tool):
    """Call one Capability Tool adapter with the model's arguments.

    The adapter validates input and output against the declared contracts and
    reuses completed results when the definition allows it.  Contract errors
    are returned to the model so it can correct the call in the same turn.
    """

    def __init__(
        self,
        definition: ToolDefinition,
        adapter: Any,
        *,
        task_id: str,
    ) -> None:
        self.name = definition.id
        self.description = "\n\n".join(
            part for part in (definition.description, definition.instructions) if part
        )
        self.side_effect = definition.side_effect
        self.parallel_safe = definition.parallel_safe
        self.input_schema = adapter.input_contract.json_schema()
        if self.input_schema.get("type") != "object":
            raise ValueError(
                f"model-visible tool {definition.id} needs an object input contract"
            )
        self._adapter = adapter
        self._task_id = task_id

    async def __call__(self, **arguments: Any) -> Any:
        try:
            outcome = await self._adapter.invoke(dict(arguments), task_id=self._task_id)
        except ContractValidationError as exc:
            return {
                "status": "error",
                "error": f"arguments or result do not match the tool contract: {exc}",
            }
        if outcome.status != "ok":
            return {"status": "error", "error": outcome.error or outcome.status}
        return outcome.result


__all__ = ["DefinitionTool"]
