"""HTTP request and response contracts for local session authentication."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class LoginRequest(BaseModel):
    """Credentials accepted to establish the local administrative session."""

    model_config = ConfigDict(extra="forbid")

    username: str
    password: SecretStr = Field(repr=False)


class SessionResponse(BaseModel):
    """The public principal information from a valid browser session."""

    authenticated: bool
    username: str
    expires_at: datetime = Field(alias="expiresAt")
