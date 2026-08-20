"""Generic recovery vocabulary shared by capabilities and runtime adapters."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RecoveryEventKind(StrEnum):
    STRUCTURED_SUBMISSION_MISSING = "structured_submission_missing"
    CONTRACT_VALIDATION_FAILED = "contract_validation_failed"
    MAX_TOKENS = "max_tokens"
    TOOL_SLICE = "tool_slice"
    NO_PROGRESS = "no_progress"
    COMPLETED_TOOL_RESULT = "completed_tool_result"
    PROVIDER_ERROR = "provider_error"


class RecoveryActionKind(StrEnum):
    CORRECT = "correct"
    CONTINUE = "continue"
    STOP = "stop"
    REUSE_RESULT = "reuse_result"
    FAIL = "fail"


class RecoveryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RecoveryEventKind
    detail: dict[str, Any] = Field(default_factory=dict)


class ProgressObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    progressed: bool
    detail: dict[str, Any] = Field(default_factory=dict)


class RecoveryState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempts: dict[RecoveryEventKind, int] = Field(default_factory=dict)


class RecoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: RecoveryEvent
    action: RecoveryActionKind
    attempt: int = Field(ge=1)
    prompt: str | None = None
    reason: str | None = None
