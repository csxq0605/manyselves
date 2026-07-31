"""Environment-backed web service settings."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class WebSettings(BaseSettings):
    """Values required to host the single local runtime."""

    data_root: Path
    initial_project_id: str
    access_token: SecretStr
    allowed_origins: list[str] = []
    sse_replay_capacity: int = 2000
    sse_client_queue_capacity: int = 500
    control_lease_seconds: int = 60
