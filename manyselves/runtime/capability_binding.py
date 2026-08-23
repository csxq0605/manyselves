"""Runtime bindings selected by file-defined Capability ownership."""

from importlib import import_module
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from manyselves.kernel.definitions import CapabilityCatalog


class CapabilityBindingError(RuntimeError):
    """Raised when a declared runtime binding cannot be constructed."""


class CapabilityRunNotFoundError(LookupError):
    """Raised by a binding when it does not own a requested Run."""


class CapabilityRunInputError(ValueError):
    """Raised when a Capability rejects generic Run input."""


class CapabilityRunStateError(RuntimeError):
    """Raised when a Capability cannot project its persisted Run state."""


class CapabilityRuntimeBinding(Protocol):
    """Application-facing operations owned by one Capability adapter."""

    capability_id: str

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]: ...

    async def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: Any,
    ) -> dict[str, Any]: ...

    def get_run(self, run_id: str) -> dict[str, Any]: ...

    def get_outputs(self, run_id: str) -> dict[str, Any]: ...

    def get_cost(self, run_id: str) -> dict[str, Any]: ...


class RuntimeBindingCatalog:
    """Bind Capability IDs and existing Runs to their runtime adapters."""

    def __init__(self) -> None:
        self._bindings: dict[str, CapabilityRuntimeBinding] = {}

    def register(self, binding: CapabilityRuntimeBinding) -> None:
        if binding.capability_id in self._bindings:
            raise CapabilityBindingError(
                f"duplicate runtime binding: {binding.capability_id}"
            )
        self._bindings[binding.capability_id] = binding

    def require(self, capability_id: str) -> CapabilityRuntimeBinding:
        try:
            return self._bindings[capability_id]
        except KeyError as exc:
            raise CapabilityBindingError(
                f"missing runtime binding: {capability_id}"
            ) from exc

    def has(self, capability_id: str) -> bool:
        return capability_id in self._bindings

    def locate_run(
        self,
        run_id: str,
    ) -> tuple[CapabilityRuntimeBinding, dict[str, Any]]:
        for capability_id in sorted(self._bindings):
            binding = self._bindings[capability_id]
            try:
                return binding, binding.get_run(run_id)
            except CapabilityRunNotFoundError:
                continue
        raise CapabilityRunNotFoundError(run_id)


def load_runtime_bindings(
    capabilities: CapabilityCatalog,
    *,
    workspace: Path,
    host: Any,
) -> RuntimeBindingCatalog:
    """Construct runtime adapters from trusted installed Capability references."""

    bindings = RuntimeBindingCatalog()
    for loaded in capabilities.all():
        reference = loaded.definition.runtime
        if reference is None:
            continue
        factory = _resolve_factory(reference)
        binding = factory(workspace=Path(workspace), host=host)
        if binding.capability_id != loaded.definition.id:
            raise CapabilityBindingError(
                f"runtime binding {binding.capability_id} does not match "
                f"Capability {loaded.definition.id}"
            )
        bindings.register(binding)
    return bindings


def _resolve_factory(reference: str):
    try:
        module_name, attribute = reference.split(":", maxsplit=1)
        module = import_module(module_name)
        factory = getattr(module, attribute)
    except (AttributeError, ImportError, ValueError) as exc:
        raise CapabilityBindingError(
            f"cannot resolve runtime binding factory: {reference}"
        ) from exc
    if not callable(factory):
        raise CapabilityBindingError(
            f"runtime binding factory is not callable: {reference}"
        )
    return factory
