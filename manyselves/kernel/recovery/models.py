"""Generic recovery vocabulary shared by capabilities and runtime adapters."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RecoveryEventKind(StrEnum):
    INVALID_STRUCTURED_OUTPUT = "invalid_structured_output"
    NATURAL_LANGUAGE_WITHOUT_SUBMISSION = "natural_language_without_submission"
    MAX_TOKENS = "max_tokens"
    TOOL_SLICE_BOUNDARY = "tool_slice_boundary"
    NO_PROGRESS = "no_progress"
    TOOL_CONTRACT_ERROR = "tool_contract_error"
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
