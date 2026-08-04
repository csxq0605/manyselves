from pathlib import Path

from manyselves.config.manager import ConfigManager
from manyselves.config.schema import Settings


def _write_config(path: Path, api_key: str | None) -> None:
    key_line = f"      api_key: {api_key}\n" if api_key is not None else ""
    path.write_text(
        "providers:\n"
        "  active: openai\n"
        "  configurations:\n"
        "    - id: openai\n"
        "      name: OpenAI\n"
        "      provider: openai\n"
        "      enabled: true\n"
        f"{key_line}",
        encoding="utf-8",
    )


def test_environment_key_is_effective_but_never_written_to_yaml(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "manyselves.config.yaml"
    _write_config(config_path, "yaml-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "env-secret")
    manager = ConfigManager(Settings(config_path=config_path))

    provider = manager.get_provider_state("openai")
    assert provider.api_key == "env-secret"
    assert provider.credential_source == "environment"
    assert "env-secret" not in repr(provider)

    manager.save_config()
    persisted = config_path.read_text(encoding="utf-8")
    assert "env-secret" not in persisted
    assert "yaml-secret" in persisted


def test_yaml_and_missing_keys_report_their_credential_sources(tmp_path: Path) -> None:
    config_path = tmp_path / "manyselves.config.yaml"
    _write_config(config_path, "yaml-secret")
    manager = ConfigManager(config_path)

    assert manager.get_provider_state("openai").credential_source == "yaml"
    manager.get_provider_state("openai").api_key = None
    manager.get_provider_state("openai").yaml_api_key = None
    manager.get_provider_state("openai").credential_source = "none"
    assert manager.get_provider_state("openai").credential_source == "none"
