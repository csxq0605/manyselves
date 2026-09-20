"""Non-secret settings DTOs."""

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from ...config.schema import CredentialSource
from ...runtime.providers.factory import ALL_PROVIDER_TYPES


class ProviderSettingsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    name: str
    preset_id: str | None = Field(alias="presetId")
    provider: str
    enabled: bool
    configured: bool
    credential_source: CredentialSource = Field(alias="credentialSource")
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
    extra_headers: dict[str, str] | None = Field(default=None, alias="extraHeaders")

    @field_validator("name", "provider", "enabled")
    @classmethod
    def reject_null_required_provider_fields(cls, value):
        if value is None:
            raise ValueError("field may be omitted but must not be null")
        return value


class SettingsDefaultsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    active_provider_id: str | None = Field(default=None, alias="activeProviderId")
    api_key: SecretStr | None = Field(default=None, alias="apiKey", repr=False)
    api_base: str | None = Field(default=None, alias="apiBase")
    model: str | None = None
    provider: str | None = None

    @field_validator("active_provider_id", "model", "provider")
    @classmethod
    def reject_explicit_null_defaults(cls, value):
        if value is None:
            raise ValueError("field may be omitted but must not be null")
        return value

    @model_validator(mode="after")
    def require_active_provider_for_provider_fields(self):
        provider_fields = {"api_base", "api_key"}
        if self.model_fields_set & provider_fields and self.active_provider_id is None:
            raise ValueError("activeProviderId is required with apiBase or apiKey")
        return self


class ProviderSettingsCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    preset_id: str | None = Field(default=None, alias="presetId")
    api_key: SecretStr | None = Field(default=None, alias="apiKey", repr=False)
    api_base: str | None = Field(default=None, alias="apiBase")
    enabled: bool = True
    default_model: str | None = Field(default=None, alias="defaultModel")
    extra_headers: dict[str, str] | None = Field(default=None, alias="extraHeaders")


class ProviderConfigurationUpsert(BaseModel):
    """Complete, atomically-applied provider configuration from the settings UI."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    preset_id: str | None = Field(default=None, alias="presetId")
    name: str = Field(min_length=1)
    protocol: str = Field(min_length=1)
    api_key: SecretStr | None = Field(default=None, alias="apiKey", repr=False)
    api_base: str | None = Field(default=None, alias="apiBase")
    default_model: str = Field(min_length=1, alias="defaultModel")
    extra_headers: dict[str, str] | None = Field(default=None, alias="extraHeaders")
    enabled: bool = True
    make_active: bool = Field(default=True, alias="makeActive")

    @field_validator("protocol")
    @classmethod
    def require_supported_protocol(cls, value: str) -> str:
        if value not in ALL_PROVIDER_TYPES:
            raise ValueError(f"unsupported protocol: {value}")
        return value


class ProviderConnectionTestRequest(BaseModel):
    """Ephemeral provider connection details; never written to configuration."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    protocol: str = Field(min_length=1)
    api_key: SecretStr | None = Field(default=None, alias="apiKey", repr=False)
    api_base: str | None = Field(default=None, alias="apiBase")
    default_model: str = Field(min_length=1, alias="defaultModel")
    extra_headers: dict[str, str] | None = Field(default=None, alias="extraHeaders")

    @field_validator("protocol")
    @classmethod
    def require_supported_protocol(cls, value: str) -> str:
        if value not in ALL_PROVIDER_TYPES:
            raise ValueError(f"unsupported protocol: {value}")
        return value


class ProviderPresetResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    provider: str
    category: str
    base_url: str = Field(alias="baseUrl")
    default_model: str = Field(alias="defaultModel")
    website_url: str = Field(alias="websiteUrl")
    description: str


class PresetListResponse(BaseModel):
    presets: list[ProviderPresetResponse]


class PresetSyncResponse(BaseModel):
    downloaded: int


class SettingsValidationResponse(BaseModel):
    valid: bool
    available_providers: list[str] = Field(alias="availableProviders")
    errors: list[str]


class ProviderConnectionTestResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ok: bool
    provider_id: str | None = Field(alias="providerId")
    model: str | None
    message: str
