"""Trusted Python operation DTOs."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PythonRunRequest(BaseModel):
    """A trusted Python operation supported only by the Linux Compose deployment."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    arguments: list[str] = Field(default_factory=list)


class OperationAcceptedResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    command_id: UUID = Field(alias="commandId")
    operation_id: str = Field(alias="operationId")
    status: Literal["accepted"] = "accepted"


class PythonOperationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    operation_id: str = Field(alias="operationId")
    path: str
    arguments: list[str]
    status: str
    stdout: str
    stderr: str
    stdout_truncated: bool = Field(alias="stdoutTruncated")
    stderr_truncated: bool = Field(alias="stderrTruncated")
    return_code: int | None = Field(alias="returnCode")
    started_at: datetime = Field(alias="startedAt")
    completed_at: datetime | None = Field(alias="completedAt")
