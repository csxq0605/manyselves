"""Shared API response schemas."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """A stable, machine-readable description of an API error."""

    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    """The common wire format for API errors."""

    model_config = ConfigDict(populate_by_name=True)

    error: ErrorDetail
    request_id: str = Field(alias="requestId")
