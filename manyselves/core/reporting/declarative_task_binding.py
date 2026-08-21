"""Bind file-defined Task policy to the current Reporting envelope."""

from __future__ import annotations

from manyselves.kernel.definitions import TaskDefinition

from .agentic_models import TaskEnvelope


def bind_declared_task(
    envelope: TaskEnvelope,
    task: TaskDefinition,
) -> TaskEnvelope:
    """Keep dynamic task content while making declared tools authoritative."""

    return envelope.model_copy(
        update={
            "constraints": list(
                dict.fromkeys([*task.constraints, *envelope.constraints])
            ),
            "allowed_tools": list(task.tools),
        }
    )
