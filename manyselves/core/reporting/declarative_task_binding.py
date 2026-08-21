"""Bind file-defined Task policy to the current Reporting envelope."""

from __future__ import annotations

import json

from manyselves.kernel.definitions import TaskDefinition

from .agentic_models import TaskEnvelope


def bind_declared_task(
    envelope: TaskEnvelope,
    task: TaskDefinition,
) -> TaskEnvelope:
    """Keep dynamic task content while making declared tools authoritative."""

    declared_policy = [f"Declared task objective: {task.objective}"]
    if task.completion:
        declared_policy.append(
            "Declared completion contract: "
            + json.dumps(
                task.completion,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    return envelope.model_copy(
        update={
            "constraints": list(
                dict.fromkeys(
                    [*declared_policy, *task.constraints, *envelope.constraints]
                )
            ),
            "allowed_tools": list(task.tools),
        }
    )
