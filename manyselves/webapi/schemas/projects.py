"""Portable project request and response schemas."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Description = Annotated[str, StringConstraints(max_length=1000)]


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project_id: str = Field(alias="projectId")
    display_name: DisplayName = Field(alias="displayName")
    description: Description


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    display_name: DisplayName = Field(alias="displayName")
    description: Description
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    display_name: str = Field(alias="displayName")
    description: str
    revision: str
    active: bool


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]
