"""End-to-end account worker, project write, and settings-key isolation."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import yaml

from manyselves.webapi.main import create_app
from manyselves.webapi.settings import WebSettings


def _write_accounts(path: Path) -> None:
    path.write_text(
        """version: 1
accounts:
  - id: account-a
    username: alice
    passwordEnv: TEST_ACCOUNT_A_PASSWORD
  - id: account-b
    username: bob
    passwordEnv: TEST_ACCOUNT_B_PASSWORD
""",
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)


async def _login_and_lease(
    client: httpx.AsyncClient,
    username: str,
    password: str,
) -> str:
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 204
    lease = await client.post(
        "/api/v1/control/lease",
        json={"clientId": f"browser-{username}"},
    )
    assert lease.status_code == 201
    return lease.json()["leaseToken"]


async def _save_mimo(
    client: httpx.AsyncClient,
    lease: str,
    *,
    api_key: str,
    model: str,
) -> httpx.Response:
    return await client.put(
        "/api/v1/settings/provider-configurations/mimo",
        headers={"X-Control-Lease-Token": lease},
        json={
            "name": "MiMo",
            "protocol": "openai",
            "apiKey": api_key,
            "apiBase": "https://api.xiaomimimo.com/v1",
            "defaultModel": model,
            "enabled": True,
            "makeActive": True,
        },
    )


@pytest.mark.asyncio
async def test_web_settings_keys_workers_and_project_writes_are_account_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts_file = tmp_path / "accounts.yaml"
    _write_accounts(accounts_file)
    monkeypatch.setenv("TEST_ACCOUNT_A_PASSWORD", "alice-password")
    monkeypatch.setenv("TEST_ACCOUNT_B_PASSWORD", "bob-password")
    monkeypatch.setenv("MIMO_API_KEY", "process-key-must-not-leak")

    data_root = tmp_path / "server-data"
    app = create_app(WebSettings(data_root=data_root, accounts_file=accounts_file))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as alice,
            httpx.AsyncClient(transport=transport, base_url="http://test") as bob,
        ):
            alice_lease = await _login_and_lease(alice, "alice", "alice-password")
            bob_lease = await _login_and_lease(bob, "bob", "bob-password")

            # Process-level dotenv/provider keys are intentionally unavailable to
            # account runtimes; only a key entered through that account's UI exists.
            assert (await alice.get("/api/v1/settings")).json()["providers"] == []
            assert (await bob.get("/api/v1/settings")).json()["providers"] == []

            alice_saved = await _save_mimo(
                alice,
                alice_lease,
                api_key="alice-api-key",
                model="mimo-v2.5",
            )
            bob_saved = await _save_mimo(
                bob,
                bob_lease,
                api_key="bob-api-key",
                model="mimo-v2.5-pro",
            )
            assert alice_saved.status_code == 200
            assert bob_saved.status_code == 200
            assert "alice-api-key" not in alice_saved.text
            assert "bob-api-key" not in bob_saved.text
            assert alice_saved.json()["defaults"]["model"] == "mimo-v2.5"
            assert bob_saved.json()["defaults"]["model"] == "mimo-v2.5-pro"

            project = {
                "projectId": "same-project-id",
                "displayName": "Same visible project",
                "description": "account-local",
            }
            alice_project = await alice.post(
                "/api/v1/projects",
                headers={"X-Control-Lease-Token": alice_lease},
                json=project,
            )
            bob_project = await bob.post(
                "/api/v1/projects",
                headers={"X-Control-Lease-Token": bob_lease},
                json=project,
            )
            assert alice_project.status_code == 201
            assert bob_project.status_code == 201

            runtimes = app.state.tenant_runtime_manager._runtimes  # noqa: SLF001
            assert runtimes["account-a"].runtime_host is not runtimes["account-b"].runtime_host
            assert runtimes["account-a"].runtime_host.bus is not runtimes["account-b"].runtime_host.bus
            assert runtimes["account-a"].event_broker is not runtimes["account-b"].event_broker
            assert runtimes["account-a"].event_store.db_path != runtimes["account-b"].event_store.db_path

    alice_root = data_root / "accounts" / "account-a"
    bob_root = data_root / "accounts" / "account-b"
    assert (alice_root / "same-project-id").is_dir()
    assert (bob_root / "same-project-id").is_dir()
    assert not alice_root.is_relative_to(bob_root)
    assert not bob_root.is_relative_to(alice_root)

    alice_config = yaml.safe_load(
        (alice_root / ".manyselves/config/manyselves.config.yaml").read_text()
    )
    bob_config = yaml.safe_load(
        (bob_root / ".manyselves/config/manyselves.config.yaml").read_text()
    )
    assert alice_config["providers"]["configurations"][0]["api_key"] == "alice-api-key"
    assert bob_config["providers"]["configurations"][0]["api_key"] == "bob-api-key"
