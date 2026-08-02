"""Transactional configuration mutation and live-runtime application."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .async_ownership import await_owned

if TYPE_CHECKING:
    from .runtime_host import RuntimeHost


class SettingsService:
    """Persist one config change, apply it live, and roll back both on failure."""

    def __init__(self, host: RuntimeHost) -> None:
        self.host = host
        self.manager = host.config_manager

    async def mutate(
        self,
        mutation: Callable[[Any], None],
        *,
        restart_reason: str | None,
    ) -> None:
        before = self.manager.config.model_copy(deep=True)
        persisted = self._capture_persisted()
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
        except BaseException as error:
            self._rollback_config(before, persisted, error)
            raise

        if restart_reason is None:
            return

        replacement = await await_owned(self.host.replace_loop_manager())
        if replacement.error is None:
            if replacement.cancellation_requested:
                raise asyncio.CancelledError
            return

        replacement_error = replacement.error
        self._rollback_config(before, persisted, replacement_error)
        recovery = await await_owned(self.host.replace_loop_manager(recovery=True))
        if recovery.error is not None:
            replacement_error.add_note(
                "Provider runtime recovery failed after configuration rollback."
            )
        raise replacement_error

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

    def _rollback_config(
        self,
        snapshot: Any,
        persisted: tuple[Path, bool, bytes] | None,
        primary_error: BaseException,
    ) -> None:
        self._restore(snapshot)
        try:
            if persisted is None:
                self.manager.save_config()
            else:
                self._restore_persisted(persisted)
        except BaseException:
            primary_error.add_note("Settings persistence rollback did not finish.")

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
