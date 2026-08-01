"""Non-secret settings DTOs."""

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class ProviderSettingsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    name: str
    provider: str
    enabled: bool
    configured: bool
    api_base: str | None = Field(alias="apiBase")
    default_model: str | None = Field(alias="defaultModel")
    active: bool


class AgentDefaultsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    model: str
    provider: str
    max_tokens: int = Field(alias="maxTokens")
    temperature: float


class SettingsResponse(BaseModel):
    providers: list[ProviderSettingsResponse]
    defaults: AgentDefaultsResponse


class ProviderSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str | None = None
    provider: str | None = None
    api_key: SecretStr | None = Field(default=None, alias="apiKey", repr=False)
    api_base: str | None = Field(default=None, alias="apiBase")
    enabled: bool | None = None
    default_model: str | None = Field(default=None, alias="defaultModel")

    @field_validator("name", "provider", "enabled")
    @classmethod
    def reject_null_required_provider_fields(cls, value):
        if value is None:
            raise ValueError("field may be omitted but must not be null")
        return value


class SettingsDefaultsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    active_provider_id: str | None = Field(default=None, alias="activeProviderId")
    model: str | None = None
    provider: str | None = None

    @field_validator("active_provider_id", "model", "provider")
    @classmethod
    def reject_explicit_null_defaults(cls, value):
        if value is None:
            raise ValueError("field may be omitted but must not be null")
        return value
