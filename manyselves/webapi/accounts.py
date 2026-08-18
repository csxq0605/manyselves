"""Server-local account identities for one shared HTTP service."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass

import yaml

from .settings import WebSettings

_ACCOUNT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    """One login identity and its stable, filesystem-safe tenant ID."""

    account_id: str
    username: str
    password: str


class AccountCatalog:
    """Read-only account lookup loaded once at service startup."""

    def __init__(self, accounts: list[AccountIdentity]) -> None:
        if not accounts:
            raise ValueError("at least one account must be configured")
        ids = [item.account_id for item in accounts]
        usernames = [item.username for item in accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("account IDs must be unique")
        if len(usernames) != len(set(usernames)):
            raise ValueError("account usernames must be unique")
        self._by_id = {item.account_id: item for item in accounts}
        self._by_username = {item.username: item for item in accounts}

    @classmethod
    def from_settings(cls, settings: WebSettings) -> "AccountCatalog":
        """Load a multi-account manifest, or retain the legacy single admin."""

        if settings.accounts_file is None:
            return cls(
                [
                    AccountIdentity(
                        account_id="default",
                        username=settings.admin_username,
                        password=settings.admin_password.get_secret_value(),
                    )
                ]
            )

        path = settings.accounts_file.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"accounts file does not exist: {path}")
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ValueError(f"accounts file must be mode 0600: {path}")
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if payload.get("version") != 1 or not isinstance(payload.get("accounts"), list):
            raise ValueError("accounts file must contain version: 1 and an accounts list")

        accounts: list[AccountIdentity] = []
        for raw in payload["accounts"]:
            if not isinstance(raw, dict):
                raise ValueError("each account must be an object")
            account_id = str(raw.get("id", "")).strip()
            username = str(raw.get("username", "")).strip()
            password_env = str(raw.get("passwordEnv", "")).strip()
            if _ACCOUNT_ID.fullmatch(account_id) is None:
                raise ValueError(f"invalid account id: {account_id!r}")
            if not username or not password_env:
                raise ValueError(f"account {account_id} requires username and passwordEnv")
            password = os.getenv(password_env)
            if not password:
                raise ValueError(
                    f"password environment variable is missing for account {account_id}: "
                    f"{password_env}"
                )
            accounts.append(AccountIdentity(account_id, username, password))
        return cls(accounts)

    @property
    def multi_account(self) -> bool:
        return len(self._by_id) > 1

    @property
    def account_ids(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    def contains(self, account_id: str) -> bool:
        return account_id in self._by_id

    def matches_principal(self, account_id: str, username: str) -> bool:
        account = self._by_id.get(account_id)
        return account is not None and secrets.compare_digest(
            account.username.encode("utf-8"), username.encode("utf-8")
        )

    def authenticate(self, username: str, password: str) -> AccountIdentity | None:
        """Resolve credentials without exposing which comparison failed."""

        account = self._by_username.get(username)
        if account is None:
            secrets.compare_digest(password.encode("utf-8"), b"invalid-account")
            return None
        if not secrets.compare_digest(
            password.encode("utf-8"),
            account.password.encode("utf-8"),
        ):
            return None
        return account
