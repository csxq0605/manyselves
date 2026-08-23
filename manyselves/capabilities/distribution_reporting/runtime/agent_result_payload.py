"""Load the existing typed AgentResult wrapper for Capability bridges.

``SubmitResultTool`` persists an :class:`AgentResult` document and publishes
the document's relative path in ``AgentResultMessage.result_path``.  This
adapter reads that existing wire shape and keeps the wrapper's typed identity
metadata beside its typed payload.  It deliberately does not add integrity,
identity, or completion policy: those checks remain with the caller and the
existing durable attempt owner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    AgentResult,
    AgentRunStatus,
    Submission,
)


@dataclass(frozen=True)
class AgentResultIdentity:
    """Typed identity/status metadata retained from one persisted result."""

    task_id: str
    run_id: str
    agent_id: str
    session_id: str
    status: AgentRunStatus


@dataclass(frozen=True)
class LoadedAgentResult:
    """Typed payload plus the original result and its identity metadata."""

    payload: Submission | None
    identity: AgentResultIdentity
    result: AgentResult


def load_agent_result_payload(
    workspace: Path,
    result_ref: str | Path,
) -> LoadedAgentResult:
    """Read one existing ``AgentResult`` wrapper from ``result_ref``.

    Relative references use the existing workspace root; absolute references
    are accepted because the current bridge decoders already receive both
    forms.  The file must contain the persisted ``AgentResult`` wrapper.  A
    naked submission JSON document is intentionally not accepted or guessed.
    """

    path = Path(result_ref)
    path = path if path.is_absolute() else Path(workspace) / path
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    result = AgentResult.model_validate(raw)
    return LoadedAgentResult(
        payload=result.payload,
        identity=AgentResultIdentity(
            task_id=result.task_id,
            run_id=result.run_id,
            agent_id=result.agent_id,
            session_id=result.session_id,
            status=result.status,
        ),
        result=result,
    )


__all__ = [
    "AgentResultIdentity",
    "LoadedAgentResult",
    "load_agent_result_payload",
]

