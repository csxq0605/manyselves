"""Pure runtime-parameter substitution for reusable file workflows."""

from copy import deepcopy
from typing import Any

from .models import WorkflowDefinition


def specialize_workflow(
    workflow: WorkflowDefinition,
    values: dict[str, Any],
    *,
    workflow_id: str | None = None,
) -> WorkflowDefinition:
    """Bind declared workflow parameters without changing the loaded template."""

    missing = set(workflow.parameters) - values.keys()
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"missing workflow parameters: {names}")
    payload = workflow.model_dump(mode="python")
    payload["id"] = workflow_id or workflow.id
    payload["state"] = _substitute(payload["state"], values)
    payload["actions"] = _substitute(payload["actions"], values)
    payload["parameters"] = []
    return WorkflowDefinition.model_validate(payload)


def _substitute(value: Any, parameters: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _substitute(item, parameters) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, parameters) for item in value]
    if not isinstance(value, str):
        return deepcopy(value)
    if value.startswith("{") and value.endswith("}"):
        parameter = value[1:-1]
        if parameter in parameters:
            return deepcopy(parameters[parameter])
    return value.format_map(parameters)
