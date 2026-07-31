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
    upload_size_limit_bytes: int = 100 * 1024 * 1024
    text_file_size_limit_bytes: int = 2 * 1024 * 1024
    preview_size_limit_bytes: int = 8 * 1024 * 1024
    preview_archive_expanded_limit_bytes: int = 32 * 1024 * 1024
    preview_row_limit: int = 200
    preview_column_limit: int = 100
    preview_block_limit: int = 500
    preview_archive_member_limit: int = 2_000
    preview_sheet_limit: int = 100
    preview_cell_character_limit: int = 4_096
    preview_csv_record_byte_limit: int = 256 * 1024
    preview_csv_field_limit: int = 10_000
    file_tree_entry_limit: int = 20_000
