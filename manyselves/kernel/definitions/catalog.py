"""Capability discovery and cross-capability workflow lookup."""

from dataclasses import dataclass
from pathlib import Path

from .loader import load_capability
from .models import CapabilityDefinition, DefinitionKind, WorkflowDefinition
from .registry import DefinitionRegistry


class CapabilityCatalogError(ValueError):
    """Raised when discovered capability definitions cannot share one catalog."""


@dataclass(frozen=True, slots=True)
class LoadedCapability:
    """One loaded capability bundle and its source definition file."""

    definition: CapabilityDefinition
    registry: DefinitionRegistry
    source: Path


class CapabilityCatalog:
    """Discover capability bundles and resolve globally addressable workflows."""

    def __init__(self) -> None:
        self._capabilities: dict[str, LoadedCapability] = {}
        self._workflows: dict[str, tuple[LoadedCapability, WorkflowDefinition]] = {}

    @classmethod
    def discover(cls, roots: list[Path] | tuple[Path, ...]) -> "CapabilityCatalog":
        catalog = cls()
        capability_files: set[Path] = set()
        for root in roots:
            source = Path(root)
            if source.is_file():
                capability_files.add(source.resolve())
                continue
            direct = source / "capability.yaml"
            if direct.is_file():
                capability_files.add(direct.resolve())
            capability_files.update(
                path.resolve()
                for path in source.glob("*/capability.yaml")
                if path.is_file()
            )
        for path in sorted(capability_files):
            catalog.load(path)
        return catalog

    def load(self, path: Path) -> LoadedCapability:
        definition, registry = load_capability(path)
        if definition.id in self._capabilities:
            previous = self._capabilities[definition.id]
            raise CapabilityCatalogError(
                f"duplicate capability id: {definition.id}: "
                f"{previous.source} and {Path(path)}"
            )
        loaded = LoadedCapability(
            definition=definition,
            registry=registry,
            source=Path(path),
        )
        workflows = tuple(registry.all(DefinitionKind.WORKFLOW))
        for workflow in workflows:
            if not isinstance(workflow, WorkflowDefinition):
                continue
            if workflow.id in self._workflows:
                owner, _previous = self._workflows[workflow.id]
                raise CapabilityCatalogError(
                    f"duplicate workflow id: {workflow.id}: "
                    f"{owner.definition.id} and {definition.id}"
                )
        self._capabilities[definition.id] = loaded
        for workflow in workflows:
            if isinstance(workflow, WorkflowDefinition):
                self._workflows[workflow.id] = (loaded, workflow)
        return loaded

    def all(self) -> tuple[LoadedCapability, ...]:
        return tuple(
            self._capabilities[capability_id]
            for capability_id in sorted(self._capabilities)
        )

    def require(self, capability_id: str) -> LoadedCapability:
        try:
            return self._capabilities[capability_id]
        except KeyError as exc:
            raise CapabilityCatalogError(
                f"unknown capability id: {capability_id}"
            ) from exc

    def require_workflow(
        self,
        workflow_id: str,
    ) -> tuple[LoadedCapability, WorkflowDefinition]:
        try:
            return self._workflows[workflow_id]
        except KeyError as exc:
            raise CapabilityCatalogError(
                f"unknown workflow id: {workflow_id}"
            ) from exc
