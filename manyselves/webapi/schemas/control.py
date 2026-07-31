"""Control-lease request and response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ...application.control import ControlLease


class _CamelCaseModel(BaseModel):
    """Accept Python names internally while preserving the public wire aliases."""

    model_config = ConfigDict(populate_by_name=True)


class LeaseAcquireRequest(_CamelCaseModel):
    """Request a new lease or renew the caller's current lease."""

    client_id: str = Field(alias="clientId", min_length=1)
    actor_id: str | None = Field(default=None, alias="actorId", min_length=1)
    lease_token: str | None = Field(default=None, alias="leaseToken")


class LeaseTokenRequest(_CamelCaseModel):
    """Supply a lease token for heartbeat and release actions."""

    lease_token: str = Field(default="", alias="leaseToken")


class LeaseResponse(_CamelCaseModel):
    """The active control lease, including its credential for future requests."""

    client_id: str = Field(alias="clientId")
    actor_id: str = Field(alias="actorId")
    lease_token: str = Field(alias="leaseToken")
    expires_at: datetime = Field(alias="expiresAt")

    @classmethod
    def from_lease(cls, lease: ControlLease) -> "LeaseResponse":
        """Translate the application lease without changing its token semantics."""
        return cls(
            client_id=lease.client_id,
            actor_id=lease.actor_id,
            lease_token=lease.token,
            expires_at=lease.expires_at,
        )
