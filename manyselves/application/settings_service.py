"""Transactional configuration mutation and live-runtime application."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.providers.base import Message
from ..core.providers.factory import ProviderFactory
from .async_ownership import await_owned
from .errors import RuntimeConsistencyFailedError

if TYPE_CHECKING:
    from .runtime_host import RuntimeHost


@dataclass(frozen=True)
class ProviderConnectionResult:
    """Secret-free result of one bounded provider probe."""

    ok: bool
    provider_id: str
    model: str | None
    message: str


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
        rollback_complete = self._rollback_config(
            before,
            persisted,
            replacement_error,
        )
        if not rollback_complete:
            raise RuntimeConsistencyFailedError() from replacement_error
        recovery = await await_owned(self.host.replace_loop_manager(recovery=True))
        if recovery.error is not None:
            replacement_error.add_note(
                "Provider runtime recovery failed after configuration rollback."
            )
            raise RuntimeConsistencyFailedError() from replacement_error
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

    async def test_provider_connection(
        self,
        provider_id: str,
        *,
        timeout_seconds: float = 15.0,
    ) -> ProviderConnectionResult:
        """Probe a provider without mutating configuration or live Runtime state."""
        provider_config = next(
            (
                item.model_copy(deep=True)
                for item in self.manager.config.providers.configurations
                if item.id == provider_id
            ),
            None,
        )
        if provider_config is None:
            raise KeyError(provider_id)
        if not provider_config.api_key:
            return ProviderConnectionResult(
                ok=False,
                provider_id=provider_id,
                model=provider_config.default_model,
                message="API key is not configured",
            )

        provider = None
        try:
            provider = ProviderFactory.create_provider(
                provider_config.provider,
                provider_config.api_key,
                provider_config.api_base,
                provider_config.default_model,
            )
            async with asyncio.timeout(timeout_seconds):
                await provider.chat(
                    [Message(role="user", content="Reply with OK.")],
                    temperature=0,
                    max_tokens=1,
                )
        except Exception:
            result = ProviderConnectionResult(
                ok=False,
                provider_id=provider_id,
                model=provider_config.default_model,
                message="Connection failed",
            )
        else:
            result = ProviderConnectionResult(
                ok=True,
                provider_id=provider_id,
                model=provider.model,
                message="Connection succeeded",
            )
        finally:
            client = getattr(provider, "client", None)
            close = getattr(client, "close", None)
            if close is not None:
                with suppress(Exception):
                    close_result = close()
                    if isawaitable(close_result):
                        async with asyncio.timeout(1.0):
                            await close_result
        return result

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
    ) -> bool:
        try:
            self._restore(snapshot)
            if persisted is None:
                self.manager.save_config()
            else:
                self._restore_persisted(persisted)
        except BaseException:
            primary_error.add_note("Settings persistence rollback did not finish.")
            return False
        return True

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
