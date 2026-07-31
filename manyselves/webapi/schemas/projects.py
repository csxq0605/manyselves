"""Portable project request and response schemas."""

from pydantic import BaseModel, ConfigDict, Field


class ProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project_id: str = Field(alias="projectId")


class ProjectResponse(BaseModel):
    id: str
    active: bool


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]
