"""Environment-backed web service settings."""

import uuid
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class WebSettings(BaseSettings):
    """Values required to host the single local runtime."""

    # 环境变量映射（支持 MANYSELVES_ 前缀）
    data_root: Path = Path(".manyselves")
    # 默认使用 UUID 作为项目 ID，如果未设置则自动生成
    initial_project_id: str = ""
    admin_username: str = "admin"
    admin_password: SecretStr = SecretStr("yuanxi@2026")
    session_ttl_seconds: int = 12 * 60 * 60
    session_cookie_secure: bool = False
    allowed_origins: list[str] = []
    sse_replay_capacity: int = 10000  # Increased from 2000 to support more events
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
    python_timeout_seconds: float = 30.0
    python_output_limit_bytes: int = 64 * 1024

    # Event log persistence
    event_db_path: Path | None = None  # None means use data_root/.manyselves/events.db
    event_log_retention_days: int = 30  # Auto-delete events older than 30 days

    def get_initial_project_id(self) -> str:
        """获取初始项目 ID，如果未设置则生成 UUID"""
        if self.initial_project_id:
            return self.initial_project_id
        # 生成新的 UUID 作为默认项目 ID
        return str(uuid.uuid4())

    class Config:
        # 支持环境变量前缀 MANYSELVES_
        # 例如：MANYSELVES_DATA_ROOT 映射到 data_root
        env_prefix = "MANYSELVES_"
