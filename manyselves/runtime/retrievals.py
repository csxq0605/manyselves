"""Events recorded when an execution takes a result instead of producing it.

An execution of a Run may take a step's result from a record instead of
executing the step, by one of three rules:

- ``same_version_result``: a model step's completed result from this same
  version (same conversation, step and input, and what the call read still
  holds);
- ``model_call_replay``: a model call replayed from the turn memo, whose
  record may come from this version or an earlier one;
- ``program_step_replay``: a program step replayed from the step memo while
  re-executing a Run in place.

Each retrieval is written to the Run's event trace as one ``result.retrieved``
event naming the step, the version executing now, the version the record came
from, and the rule.  The event's ``workflow_id`` and ``action_id`` are those of
the step's own ``action.*`` events (the subworkflow, branch or item the step
belongs to), so ``(workflow_id, action_id)`` locates the step.  Counting these
events tells the kinds and origins of the results an execution did not produce
itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .workflow_host import WorkflowEventSink, WorkflowRuntimeEvent, executing_workflow_id

RESULT_RETRIEVED = "result.retrieved"
SAME_VERSION_RESULT = "same_version_result"
MODEL_CALL_REPLAY = "model_call_replay"
PROGRAM_STEP_REPLAY = "program_step_replay"


class RetrievalLog:
    """Write one event per result an execution took from a record."""

    def __init__(
        self,
        events: WorkflowEventSink,
        *,
        run_id: str,
        workflow_id: str,
        version: int | None,
    ) -> None:
        self._events = events
        self.run_id = run_id
        # The Root workflow; used only when no action is executing.
        self.workflow_id = workflow_id
        self.version = version

    def record(
        self,
        *,
        step: str,
        rule: str,
        source_version: int | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self._events.append(
            WorkflowRuntimeEvent(
                kind=RESULT_RETRIEVED,
                run_id=self.run_id,
                workflow_id=executing_workflow_id() or self.workflow_id,
                action_id=step,
                data={
                    "rule": rule,
                    "version": self.version,
                    "source_version": source_version,
                    **dict(detail or {}),
                },
            )
        )


__all__ = [
    "MODEL_CALL_REPLAY",
    "PROGRAM_STEP_REPLAY",
    "RESULT_RETRIEVED",
    "SAME_VERSION_RESULT",
    "RetrievalLog",
]
