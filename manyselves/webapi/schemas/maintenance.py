"""Maintenance quiesce DTOs."""

from pydantic import BaseModel, ConfigDict, Field


class MaintenanceResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    quiesced: bool
    maintenance_token: str | None = Field(default=None, alias="maintenanceToken", repr=False)


class MaintenanceReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    maintenance_token: str = Field(alias="maintenanceToken", repr=False)
