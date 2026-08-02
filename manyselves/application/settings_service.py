"""Transactional configuration mutation and live-runtime application."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


class SettingsService:
    """Persist one config change, apply it live, and roll back both on failure."""

    def __init__(self, manager: Any, backend: Any) -> None:
        self.manager = manager
        self.backend = backend

    async def mutate(
        self,
        mutation: Callable[[Any], None],
        *,
        restart_reason: str | None,
    ) -> None:
        before = self.manager.config.model_copy(deep=True)
        persisted = self._capture_persisted()
        restart: Callable[[str], Any] | None = None
        restart_attempted = False
        try:
            mutation(self.manager.config)
            valid, _available, errors = self.validate()
            structural_errors = [
                item
                for item in errors
                if item != "At least one enabled provider must be configured"
            ]
            if not valid and structural_errors:
                raise ValueError("; ".join(structural_errors))
            self.manager.save_config()
            if restart_reason is not None:
                restart = getattr(self.backend, "restart_agents_and_wait", None)
                if not callable(restart):
                    restart = self.backend.restart_agents
                restart_attempted = True
                await restart(restart_reason)
        except BaseException as error:
            self._restore(before)
            try:
                if persisted is None:
                    self.manager.save_config()
                else:
                    self._restore_persisted(persisted)
            except BaseException as rollback_error:
                error.add_note(f"Settings persistence rollback failed: {rollback_error!r}")
            if restart_attempted and restart is not None:
                try:
                    await restart("settings_rollback")
                except BaseException as rollback_error:
                    error.add_note(f"Settings runtime rollback failed: {rollback_error!r}")
            raise

    def validate(self) -> tuple[bool, list[str], list[str]]:
        config = self.manager.config
        errors: list[str] = []
        ids = [item.id for item in config.providers.configurations]
        if len(ids) != len(set(ids)):
            errors.append("Provider IDs must be unique")
        if config.providers.active is not None and config.providers.active not in ids:
            errors.append("Active provider does not exist")
        available = sorted(
            {
                item.provider
                for item in config.providers.configurations
                if item.enabled and bool(item.api_key)
            }
        )
        if not available:
            errors.append("At least one enabled provider must be configured")
        return not errors, available, errors

    def _restore(self, snapshot: Any) -> None:
        if hasattr(self.manager, "_config"):
            self.manager._config = snapshot  # noqa: SLF001
        else:
            self.manager.config = snapshot

    def _capture_persisted(self) -> tuple[Path, bool, bytes] | None:
        settings = getattr(self.manager, "_settings", None)
        config_path = getattr(settings, "config_path", None)
        if config_path is None:
            return None
        path = Path(config_path)
        existed = path.exists()
        return path, existed, path.read_bytes() if existed else b""

    @staticmethod
    def _restore_persisted(snapshot: tuple[Path, bool, bytes]) -> None:
        path, existed, content = snapshot
        if existed:
            path.write_bytes(content)
        else:
            path.unlink(missing_ok=True)
