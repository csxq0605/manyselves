"""Masked provider settings and lease-controlled updates."""

from fastapi import APIRouter, Depends, Request

from ...application.control import ControlLeaseRequired
from ...application.errors import MaintenanceQuiescedError, RuntimeNotReadyError
from ..errors import ApiError
from ..schemas.settings import (
    AgentDefaultsResponse,
    ProviderSettingsResponse,
    ProviderSettingsUpdate,
    SettingsDefaultsUpdate,
    SettingsResponse,
)
from ..security import require_control_lease_header, require_deployment_access

router = APIRouter(prefix="/settings")


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
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    manager = request.app.state.runtime_host.config_manager
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            if body.active_provider_id is not None:
                if not any(item.id == body.active_provider_id for item in manager.config.providers.configurations):
                    raise KeyError(body.active_provider_id)
                manager.config.providers.active = body.active_provider_id
            if body.model is not None:
                manager.config.agents.defaults.model = body.model
            if body.provider is not None:
                manager.config.agents.defaults.provider = body.provider
            manager.save_config()
            return _settings(manager)
    except Exception as error:
        raise _error(error) from error


@router.patch("/providers/{provider_id}", response_model=SettingsResponse)
async def update_provider(
    provider_id: str,
    body: ProviderSettingsUpdate,
    request: Request,
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    manager = request.app.state.runtime_host.config_manager
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            provider = next(
                (item for item in manager.config.providers.configurations if item.id == provider_id),
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
                provider.api_key = secret.get_secret_value() if secret is not None else ""
            manager.save_config()
            return _settings(manager)
    except Exception as error:
        raise _error(error) from error
