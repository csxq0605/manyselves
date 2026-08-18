"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "manyselves.config.yaml"

CredentialSource = Literal["none", "yaml", "environment"]


class Base(BaseModel):
    """Base model with camelCase alias support."""

    model_config = ConfigDict(
        alias_generator=lambda x: "".join(
            word.capitalize() for word in x.split("_")
        ),
        populate_by_name=True,
    )


class ApiConfig(Base):
    """A single API provider configuration.

    Replaces the old fixed-slot ProviderConfig with an identity-based
    config that users can add/remove freely, inspired by cc-switch's
    multi-configuration provider system.
    """

    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    name: str = "Unnamed"
    preset_id: str | None = None
    provider: str = "custom"  # anthropic | openai | google | deepseek | openrouter | groq | custom
    api_key: str | None = Field(default=None, repr=False)
    api_base: str | None = None
    enabled: bool = True
    default_model: str | None = None
    extra_headers: dict[str, str] | None = None
    credential_source: CredentialSource = Field(default="none", exclude=True)
    yaml_api_key: str | None = Field(default=None, exclude=True, repr=False)

    def model_post_init(self, __context: Any) -> None:
        """Remember the persisted secret before environment overrides are applied."""
        if self.yaml_api_key is None and self.api_key:
            self.yaml_api_key = self.api_key
        if self.credential_source == "none" and self.api_key:
            self.credential_source = "yaml"


class ProvidersConfig(Base):
    """List of API provider configurations."""

    configurations: list[ApiConfig] = Field(default_factory=list)
    active: str | None = None  # ID of currently active configuration


class MinerUAPIConfig(Base):
    """MinerU CLI configuration for document parsing."""

    timeout: int = 300
    enabled: bool = True
    validate_on_startup: bool = True


class AgentDefaults(Base):
    """Default agent configuration."""

    model: str = "mimo-v2.5-pro"
    provider: str = "openai"
    max_tokens: int = 8192
    temperature: float = 0.1
    max_tool_iterations: int = 200
    max_tool_calls_per_round: int = Field(default=4, ge=1)
    max_tool_result_chars: int = Field(default=6000, ge=512)
    working_memory_tokens: int = Field(default=36000, ge=4096)
    timezone: str = "Asia/Shanghai"
    prompt_templates_dir: str = "templates/agents"


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class AppConfig(Base):
    """Main application configuration."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    mineru_api: MinerUAPIConfig = Field(default_factory=MinerUAPIConfig)


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    # Environment-based API key overrides
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
    mimo_api_key: str | None = Field(default=None, alias="MIMO_API_KEY")
    mimo_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MANYSELVES_BOOTSTRAP_MODEL", "MIMO_MODEL"),
    )
    mimo_api_base: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MANYSELVES_BOOTSTRAP_API_BASE", "MIMO_API_BASE"),
    )

    config_path: Path = Field(default_factory=lambda: DEFAULT_CONFIG_PATH)

    def load_config(self) -> AppConfig:
        """Load configuration from YAML, migrating old format if needed."""
        import yaml

        config_data: dict = {}
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}

        # Migrate old fixed-slot providers format
        providers_data = config_data.get("providers", {})
        if providers_data and "configurations" not in providers_data:
            config_data["providers"] = self._migrate_providers(providers_data)

        config = AppConfig(**config_data)

        # Override API keys from environment variables
        self._apply_env_overrides(config)

        return config

    def _migrate_providers(self, old_providers: dict) -> dict:
        """Migrate old fixed-slot provider format to list-based format."""
        _names = {
            "anthropic": "Anthropic",
            "openai": "OpenAI",
            "google": "Google Gemini",
            "deepseek": "DeepSeek",
            "openrouter": "OpenRouter",
            "groq": "Groq",
            "custom": "Custom",
        }
        configurations = []
        for name in _names:
            data = old_providers.get(name, {})
            if isinstance(data, dict):
                configurations.append({
                    "id": f"{name}-default",
                    "name": _names[name],
                    "provider": name,
                    "api_key": data.get("api_key"),
                    "api_base": data.get("api_base"),
                    "enabled": data.get("enabled", True),
                    "default_model": data.get("default_model"),
                })

        active = None
        for cfg in configurations:
            if cfg.get("enabled") and cfg.get("api_key"):
                active = cfg["id"]
                break

        return {"configurations": configurations, "active": active}

    def _apply_env_overrides(self, config: AppConfig) -> None:
        """Apply environment variable API keys to matching configurations."""
        env_map: dict[str, str | None] = {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "google": self.google_api_key,
            "deepseek": self.deepseek_api_key,
            "openrouter": self.openrouter_api_key,
            "groq": self.groq_api_key,
        }
        for cfg in config.providers.configurations:
            env_key = env_map.get(cfg.provider)
            if env_key:
                cfg.api_key = env_key
                cfg.credential_source = "environment"
            if self._is_mimo_config(cfg):
                if self.mimo_api_key:
                    cfg.api_key = self.mimo_api_key
                    cfg.credential_source = "environment"
                if self.mimo_model:
                    cfg.default_model = self.mimo_model
                if self.mimo_api_base:
                    cfg.api_base = self.mimo_api_base
        active = next(
            (
                item
                for item in config.providers.configurations
                if item.id == config.providers.active
            ),
            None,
        )
        if active is not None and self._is_mimo_config(active):
            if self.mimo_model:
                config.agents.defaults.model = self.mimo_model
            config.agents.defaults.provider = active.provider

    @staticmethod
    def _is_mimo_config(config: ApiConfig) -> bool:
        model = (config.default_model or "").strip().casefold().rsplit("/", 1)[-1]
        api_base = (config.api_base or "").strip().casefold()
        return model in {"mimo-v2.5", "mimo-v2.5-pro"} or "xiaomimimo.com" in api_base

    def validate_api_keys(self, config: AppConfig) -> list[str]:
        """Validate that at least one enabled configuration has an API key."""
        available: list[str] = []
        for cfg in config.providers.configurations:
            if cfg.enabled and cfg.api_key:
                available.append(cfg.provider)
        return available
