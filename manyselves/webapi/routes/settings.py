"""Masked provider settings and transactional lease-controlled updates."""

from fastapi import APIRouter, Depends, Request

from ...application.async_ownership import to_thread_non_abandoning
from ...application.control import ControlLeaseRequired
from ...application.errors import (
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
    ProviderPresetResponse,
    ProviderSettingsCreate,
    ProviderSettingsResponse,
    ProviderSettingsUpdate,
    SettingsDefaultsUpdate,
    SettingsResponse,
    SettingsValidationResponse,
)
from ..security import require_authenticated_session, require_control_lease_header

router = APIRouter(prefix="/settings", dependencies=[Depends(require_authenticated_session)])


def _service(request: Request) -> SettingsService:
    host = request.app.state.runtime_host
    return SettingsService(host)


def _settings(manager) -> SettingsResponse:
    config = manager.config
    defaults = config.agents.defaults
    return SettingsResponse(
        providers=[
            ProviderSettingsResponse(
                id=item.id,
                name=item.name,
                provider=item.provider,
                enabled=item.enabled,
                configured=bool(item.api_key),
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


@router.get("", response_model=SettingsResponse)
async def get_settings(request: Request):
    facade = request.app.state.runtime_facade
    async with facade.read_transaction():
        return _settings(request.app.state.runtime_host.config_manager)


@router.patch("", response_model=SettingsResponse)
async def update_defaults(
    body: SettingsDefaultsUpdate,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
):
    manager = request.app.state.runtime_host.config_manager
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            def mutation(config) -> None:
                if body.active_provider_id is not None:
                    if not any(
                        item.id == body.active_provider_id
                        for item in config.providers.configurations
                    ):
                        raise KeyError(body.active_provider_id)
                    config.providers.active = body.active_provider_id
                if body.model is not None:
                    config.agents.defaults.model = body.model
                if body.provider is not None:
                    config.agents.defaults.provider = body.provider

            restart = (
                "provider_defaults_changed"
                if body.model_fields_set & {"active_provider_id", "provider"}
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
    manager = request.app.state.runtime_host.config_manager
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
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
                    provider.api_key = (
                        secret.get_secret_value() if secret is not None else ""
                    )

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
    manager = request.app.state.runtime_host.config_manager
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            secret = body.api_key.get_secret_value() if body.api_key is not None else None

            def mutation(config) -> None:
                provider = ApiConfig(
                    name=body.name,
                    provider=body.provider,
                    api_key=secret,
                    api_base=body.api_base,
                    enabled=body.enabled,
                    default_model=body.default_model,
                )
                config.providers.configurations.append(provider)
                if config.providers.active is None and provider.enabled and provider.api_key:
                    config.providers.active = provider.id

            await _service(request).mutate(
                mutation, restart_reason="provider_created"
            )
            return _settings(manager)
    except Exception as error:
        raise _error(error) from error


@router.delete("/providers/{provider_id}", response_model=SettingsResponse)
async def remove_provider(
    provider_id: str,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
):
    manager = request.app.state.runtime_host.config_manager
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            def mutation(config) -> None:
                existing = list(config.providers.configurations)
                if not any(item.id == provider_id for item in existing):
                    raise KeyError(provider_id)
                config.providers.configurations = [
                    item for item in existing if item.id != provider_id
                ]
                if config.providers.active == provider_id:
                    replacement = next(
                        (
                            item.id
                            for item in config.providers.configurations
                            if item.enabled and item.api_key
                        ),
                        None,
                    )
                    config.providers.active = replacement

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
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
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
    facade = request.app.state.runtime_facade
    async with facade.read_transaction():
        valid, available, errors = _service(request).validate()
        return SettingsValidationResponse(
            valid=valid,
            availableProviders=available,
            errors=errors,
        )
