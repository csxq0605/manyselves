"""Masked provider settings and transactional lease-controlled updates."""

from fastapi import APIRouter, Depends, Request

from ...application.async_ownership import to_thread_non_abandoning
from ...application.control import ControlLeaseRequired
from ...application.errors import (
    CredentialManagedByEnvironmentError,
    MaintenanceQuiescedError,
    RuntimeConsistencyFailedError,
    RuntimeNotReadyError,
)
from ...application.settings_service import SettingsService
from ...config.presets import load_presets
from ...config.schema import ApiConfig
from ...core.preset_sync import SyncError, sync_presets
from ..errors import ApiError
from ..schemas.settings import (
    AgentDefaultsResponse,
    PresetListResponse,
    PresetSyncResponse,
    ProviderConfigurationUpsert,
    ProviderConnectionTestRequest,
    ProviderConnectionTestResponse,
    ProviderPresetResponse,
    ProviderSettingsCreate,
    ProviderSettingsResponse,
    ProviderSettingsUpdate,
    SettingsDefaultsUpdate,
    SettingsResponse,
    SettingsValidationResponse,
)
from ..security import require_authenticated_session, require_control_lease_header
from ..tenant_runtime import request_runtime_state

router = APIRouter(prefix="/settings", dependencies=[Depends(require_authenticated_session)])


def _service(request: Request) -> SettingsService:
    host = request_runtime_state(request).runtime_host
    return SettingsService(host)


def _settings(manager) -> SettingsResponse:
    config = manager.config
    defaults = config.agents.defaults
    return SettingsResponse(
        providers=[
            ProviderSettingsResponse(
                id=item.id,
                name=item.name,
                presetId=item.preset_id,
                provider=item.provider,
                enabled=item.enabled,
                configured=bool(item.api_key),
                credentialSource=item.credential_source,
                apiBase=item.api_base,
                defaultModel=item.default_model,
                active=item.id == config.providers.active,
            )
            for item in config.providers.configurations
        ],
        defaults=AgentDefaultsResponse(
            model=defaults.model,
            provider=defaults.provider,
            maxTokens=defaults.max_tokens,
            temperature=defaults.temperature,
        ),
    )


def _error(error: Exception) -> ApiError:
    if isinstance(error, KeyError):
        return ApiError(status_code=404, code="PROVIDER_NOT_FOUND", message="Provider was not found", retryable=False)
    if isinstance(error, CredentialManagedByEnvironmentError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=False)
    if isinstance(error, ValueError):
        return ApiError(status_code=422, code="INVALID_SETTINGS", message=str(error), retryable=False)
    if isinstance(error, ControlLeaseRequired):
        return ApiError(status_code=423, code=error.code, message=str(error), retryable=False)
    if isinstance(error, RuntimeNotReadyError):
        return ApiError(status_code=503, code=error.code, message=str(error), retryable=True)
    if isinstance(error, RuntimeConsistencyFailedError):
        return ApiError(status_code=500, code=error.code, message=str(error), retryable=False)
    if isinstance(error, MaintenanceQuiescedError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=True)
    raise error


def _replace_provider_secret(provider, secret) -> None:
    if provider.credential_source == "environment":
        raise CredentialManagedByEnvironmentError()
    value = secret.get_secret_value() if secret is not None else None
    provider.api_key = value
    provider.yaml_api_key = value
    provider.credential_source = "yaml" if value else "none"


def _is_usable_provider(provider) -> bool:
    return provider.enabled and bool(provider.api_key)


def _sync_defaults_with_provider(config, provider) -> None:
    config.agents.defaults.model = provider.default_model or ""
    config.agents.defaults.provider = provider.provider


def _replace_inactive_active_provider(config, removed_provider_id: str) -> None:
    replacement = next(
        (
            item
            for item in config.providers.configurations
            if item.id != removed_provider_id and _is_usable_provider(item)
        ),
        None,
    )
    config.providers.active = replacement.id if replacement is not None else None
    if replacement is not None:
        _sync_defaults_with_provider(config, replacement)


@router.get("", response_model=SettingsResponse)
async def get_settings(request: Request):
    state = request_runtime_state(request)
    facade = state.runtime_facade
    async with facade.read_transaction():
        return _settings(state.runtime_host.config_manager)


@router.patch("", response_model=SettingsResponse)
async def update_defaults(
    body: SettingsDefaultsUpdate,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
):
    state = request_runtime_state(request)
    manager = state.runtime_host.config_manager
    try:
        async with state.runtime_facade.mutation_transaction(lease_token):
            def mutation(config) -> None:
                selected = None
                if body.active_provider_id is not None:
                    selected = next(
                        (
                            item
                            for item in config.providers.configurations
                            if item.id == body.active_provider_id
                        ),
                        None,
                    )
                    if selected is None:
                        raise KeyError(body.active_provider_id)
                    config.providers.active = body.active_provider_id
                    if "api_base" in body.model_fields_set:
                        selected.api_base = body.api_base
                    if "api_key" in body.model_fields_set:
                        _replace_provider_secret(selected, body.api_key)
                    if body.model is not None:
                        selected.default_model = body.model
                if body.model is not None:
                    config.agents.defaults.model = body.model
                if body.provider is not None:
                    config.agents.defaults.provider = body.provider

            provider_fields = {"active_provider_id", "api_base", "api_key", "provider"}
            provider_model_changed = (
                body.active_provider_id is not None
                and "model" in body.model_fields_set
            )
            restart = (
                "provider_defaults_changed"
                if body.model_fields_set & provider_fields or provider_model_changed
                else None
            )
            await _service(request).mutate(mutation, restart_reason=restart)
            return _settings(manager)
    except Exception as error:
        raise _error(error) from error


@router.patch("/providers/{provider_id}", response_model=SettingsResponse)
async def update_provider(
    provider_id: str,
    body: ProviderSettingsUpdate,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
):
    state = request_runtime_state(request)
    manager = state.runtime_host.config_manager
    try:
        async with state.runtime_facade.mutation_transaction(lease_token):
            def mutation(config) -> None:
                provider = next(
                    (
                        item
                        for item in config.providers.configurations
                        if item.id == provider_id
                    ),
                    None,
                )
                if provider is None:
                    raise KeyError(provider_id)
                updates = body.model_dump(exclude_unset=True)
                secret_present = "api_key" in updates
                secret = updates.pop("api_key", None)
                for key, value in updates.items():
                    setattr(provider, key, value)
                if secret_present:
                    _replace_provider_secret(provider, secret)
                if config.providers.active == provider.id:
                    if _is_usable_provider(provider):
                        _sync_defaults_with_provider(config, provider)
                    else:
                        _replace_inactive_active_provider(config, provider.id)

            restart_fields = {
                "provider",
                "api_key",
                "api_base",
                "enabled",
                "default_model",
            }
            restart = (
                "provider_configuration_changed"
                if body.model_fields_set & restart_fields
                else None
            )
            await _service(request).mutate(mutation, restart_reason=restart)
            return _settings(manager)
    except Exception as error:
        raise _error(error) from error


@router.post("/providers", response_model=SettingsResponse, status_code=201)
async def create_provider(
    body: ProviderSettingsCreate,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
):
    state = request_runtime_state(request)
    manager = state.runtime_host.config_manager
    try:
        async with state.runtime_facade.mutation_transaction(lease_token):
            secret = body.api_key.get_secret_value() if body.api_key is not None else None

            def mutation(config) -> None:
                provider = ApiConfig(
                    name=body.name,
                    provider=body.provider,
                    preset_id=body.preset_id,
                    api_key=secret,
                    api_base=body.api_base,
                    enabled=body.enabled,
                    default_model=body.default_model,
                    extra_headers=body.extra_headers,
                )
                config.providers.configurations.append(provider)
                if config.providers.active is None and provider.enabled and provider.api_key:
                    config.providers.active = provider.id
                    _sync_defaults_with_provider(config, provider)

            await _service(request).mutate(
                mutation, restart_reason="provider_created"
            )
            return _settings(manager)
    except Exception as error:
        raise _error(error) from error


@router.put(
    "/provider-configurations/{provider_config_id}",
    response_model=SettingsResponse,
)
async def upsert_provider_configuration(
    provider_config_id: str,
    body: ProviderConfigurationUpsert,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
) -> SettingsResponse:
    """Save one complete provider configuration and apply it live atomically."""
    state = request_runtime_state(request)
    manager = state.runtime_host.config_manager
    try:
        async with state.runtime_facade.mutation_transaction(lease_token):
            def mutation(config) -> None:
                if not provider_config_id.strip():
                    raise ValueError("provider configuration ID cannot be empty")

                provider = next(
                    (
                        item
                        for item in config.providers.configurations
                        if item.id == provider_config_id
                    ),
                    None,
                )
                if provider is None and body.preset_id is not None:
                    provider = next(
                        (
                            item
                            for item in config.providers.configurations
                            if item.preset_id == body.preset_id
                        ),
                        None,
                    )
                if provider is None:
                    provider = ApiConfig(id=provider_config_id)
                    config.providers.configurations.append(provider)

                provider.preset_id = body.preset_id
                provider.name = body.name
                provider.provider = body.protocol
                provider.api_base = body.api_base
                provider.default_model = body.default_model
                provider.extra_headers = body.extra_headers
                provider.enabled = body.enabled
                if "api_key" in body.model_fields_set:
                    _replace_provider_secret(provider, body.api_key)

                if body.make_active:
                    if not _is_usable_provider(provider):
                        raise ValueError(
                            "An active provider must be enabled and have an API key"
                        )
                    config.providers.active = provider.id
                    _sync_defaults_with_provider(config, provider)
                elif config.providers.active == provider.id:
                    if _is_usable_provider(provider):
                        _sync_defaults_with_provider(config, provider)
                    else:
                        _replace_inactive_active_provider(config, provider.id)

            await _service(request).mutate(
                mutation,
                restart_reason="provider_configuration_changed",
            )
            return _settings(manager)
    except Exception as error:
        raise _error(error) from error


@router.post(
    "/provider-configurations/test",
    response_model=ProviderConnectionTestResponse,
)
async def test_unsaved_provider_configuration(
    body: ProviderConnectionTestRequest,
    request: Request,
) -> ProviderConnectionTestResponse:
    """Test draft connection details without writing config or rebuilding runtime."""
    try:
        api_key = body.api_key.get_secret_value() if body.api_key is not None else None
        provider = ApiConfig(
            name="Connection test",
            provider=body.protocol,
            api_key=api_key,
            api_base=body.api_base,
            default_model=body.default_model,
            extra_headers=body.extra_headers,
        )
        async with request_runtime_state(request).runtime_facade.read_transaction():
            result = await _service(request).test_provider_configuration(provider)
        return ProviderConnectionTestResponse(
            ok=result.ok,
            providerId=result.provider_id,
            model=result.model,
            message=result.message,
        )
    except Exception as error:
        raise _error(error) from error


@router.post(
    "/providers/{provider_id}/test",
    response_model=ProviderConnectionTestResponse,
)
async def test_provider_connection(
    provider_id: str,
    request: Request,
) -> ProviderConnectionTestResponse:
    try:
        async with request_runtime_state(request).runtime_facade.read_transaction():
            result = await _service(request).test_provider_connection(provider_id)
        return ProviderConnectionTestResponse(
            ok=result.ok,
            providerId=result.provider_id,
            model=result.model,
            message=result.message,
        )
    except Exception as error:
        raise _error(error) from error


@router.delete("/providers/{provider_id}", response_model=SettingsResponse)
async def remove_provider(
    provider_id: str,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
):
    state = request_runtime_state(request)
    manager = state.runtime_host.config_manager
    try:
        async with state.runtime_facade.mutation_transaction(lease_token):
            def mutation(config) -> None:
                existing = list(config.providers.configurations)
                if not any(item.id == provider_id for item in existing):
                    raise KeyError(provider_id)
                config.providers.configurations = [
                    item for item in existing if item.id != provider_id
                ]
                if config.providers.active == provider_id:
                    _replace_inactive_active_provider(config, provider_id)

            await _service(request).mutate(
                mutation, restart_reason="provider_removed"
            )
            return _settings(manager)
    except Exception as error:
        raise _error(error) from error


@router.get("/presets", response_model=PresetListResponse)
async def list_provider_presets() -> PresetListResponse:
    return PresetListResponse(
        presets=[
            ProviderPresetResponse(
                id=item.id,
                name=item.name,
                provider=item.provider,
                category=item.category,
                baseUrl=item.base_url,
                defaultModel=item.default_model,
                websiteUrl=item.website_url,
                description=item.description,
            )
            for item in load_presets()
        ]
    )


@router.post("/presets/sync", response_model=PresetSyncResponse)
async def synchronize_provider_presets(
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
) -> PresetSyncResponse:
    try:
        async with request_runtime_state(request).runtime_facade.mutation_transaction(lease_token):
            downloaded = await to_thread_non_abandoning(sync_presets)
    except (
        ControlLeaseRequired,
        RuntimeNotReadyError,
        MaintenanceQuiescedError,
    ) as error:
        raise _error(error) from error
    except SyncError as error:
        raise ApiError(
            status_code=503,
            code="PRESET_SYNC_FAILED",
            message="Provider presets could not be synchronized",
            retryable=True,
        ) from error
    return PresetSyncResponse(downloaded=downloaded)


@router.post("/validate", response_model=SettingsValidationResponse)
async def validate_settings(request: Request) -> SettingsValidationResponse:
    facade = request_runtime_state(request).runtime_facade
    async with facade.read_transaction():
        valid, available, errors = _service(request).validate()
        return SettingsValidationResponse(
            valid=valid,
            availableProviders=available,
            errors=errors,
        )
