import os
from pathlib import Path

import yaml

from scripts.init_multi_account_data import initialize_multi_account_data


ACCOUNTS_TEMPLATE = Path("deploy/accounts.example.yaml")
CONFIG_TEMPLATE = Path("deploy/config/manyselves.account-default.yaml")


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_initializer_creates_isolated_accounts_with_mimo_defaults(tmp_path: Path) -> None:
    initialize_multi_account_data(tmp_path, ACCOUNTS_TEMPLATE, CONFIG_TEMPLATE)

    manifest = tmp_path / ".manyselves" / "accounts.yaml"
    assert _load_yaml(manifest) == {
        "version": 1,
        "accounts": [
            {
                "id": "admin",
                "username": "admin",
                "passwordEnv": "MANYSELVES_ACCOUNT_ADMIN_PASSWORD",
            },
            {
                "id": "yuanxi_001",
                "username": "yuanxi_001",
                "passwordEnv": "MANYSELVES_ACCOUNT_YUANXI_001_PASSWORD",
            },
            {
                "id": "yuanxi_002",
                "username": "yuanxi_002",
                "passwordEnv": "MANYSELVES_ACCOUNT_YUANXI_002_PASSWORD",
            },
        ],
    }
    if os.name != "nt":
        assert manifest.stat().st_mode & 0o777 == 0o600

    for account_id in ("admin", "yuanxi_001", "yuanxi_002"):
        config_path = (
            tmp_path
            / "accounts"
            / account_id
            / ".manyselves"
            / "config"
            / "manyselves.config.yaml"
        )
        config = _load_yaml(config_path)
        provider = config["providers"]["configurations"][0]
        assert config["providers"]["active"] == "mimo-token-plan-cn"
        assert config["agents"]["defaults"]["provider"] == "anthropic"
        assert config["agents"]["defaults"]["model"] == "mimo-v2.5-pro"
        assert provider == {
            "id": "mimo-token-plan-cn",
            "name": "Xiaomi MiMo Token Plan (China)",
            "presetId": "anthropic-xiaomi-mimo-token-plan-china",
            "provider": "anthropic",
            "apiKey": None,
            "apiBase": "https://token-plan-cn.xiaomimimo.com/anthropic",
            "defaultModel": "mimo-v2.5-pro",
            "enabled": True,
        }
        if os.name != "nt":
            assert config_path.stat().st_mode & 0o777 == 0o600


def test_initializer_preserves_existing_data_and_migrates_legacy_admin_once(
    tmp_path: Path,
) -> None:
    legacy_project = tmp_path / "default"
    (legacy_project / "Inputs").mkdir(parents=True)
    (legacy_project / "Inputs" / "brief.md").write_text("legacy", encoding="utf-8")
    legacy_config = tmp_path / "manyselves.config.yaml"
    legacy_config.write_text(
        "providers:\n  active: existing-provider\n  configurations: []\n",
        encoding="utf-8",
    )

    initialize_multi_account_data(tmp_path, ACCOUNTS_TEMPLATE, CONFIG_TEMPLATE)

    admin_root = tmp_path / "accounts" / "admin"
    assert (legacy_project / "Inputs" / "brief.md").read_text("utf-8") == "legacy"
    assert (admin_root / "default" / "Inputs" / "brief.md").read_text("utf-8") == "legacy"
    admin_config = admin_root / ".manyselves" / "config" / "manyselves.config.yaml"
    assert _load_yaml(admin_config)["providers"]["active"] == "existing-provider"

    yuanxi_config = (
        tmp_path
        / "accounts"
        / "yuanxi_001"
        / ".manyselves"
        / "config"
        / "manyselves.config.yaml"
    )
    yuanxi_config.write_text("preserved: true\n", encoding="utf-8")

    initialize_multi_account_data(tmp_path, ACCOUNTS_TEMPLATE, CONFIG_TEMPLATE)

    assert yuanxi_config.read_text("utf-8") == "preserved: true\n"
    assert legacy_config.is_file()


def test_initializer_writes_the_configured_accounts_file(tmp_path: Path) -> None:
    accounts_file = tmp_path / "runtime" / "accounts.yaml"

    initialize_multi_account_data(
        tmp_path,
        ACCOUNTS_TEMPLATE,
        CONFIG_TEMPLATE,
        accounts_file=accounts_file,
    )

    assert _load_yaml(accounts_file)["accounts"][0]["id"] == "admin"
    assert not (tmp_path / ".manyselves" / "accounts.yaml").exists()
