"""In-memory registry for loaded declarative definitions."""

from collections.abc import Iterable

from .models import (
    AgentDefinition,
    CapabilityDefinition,
    Definition,
    DefinitionKind,
    InteractionDefinition,
    OutputDefinition,
    TaskDefinition,
    ToolDefinition,
    WorkflowDefinition,
)


class DuplicateDefinitionError(ValueError):
    """Raised when the same definition kind and ID is registered twice."""


class DefinitionReferenceError(ValueError):
    """Raised when a definition references an unknown typed definition."""


class DefinitionRegistry:
    """Registry keyed by definition kind and stable external ID."""

    def __init__(self) -> None:
        self._definitions: dict[DefinitionKind, dict[str, Definition]] = {
            kind: {} for kind in DefinitionKind
        }

    def register(self, definition: Definition) -> None:
        by_id = self._definitions[DefinitionKind(definition.kind)]
        if definition.id in by_id:
            raise DuplicateDefinitionError(
                f"duplicate definition: {definition.kind}:{definition.id}"
            )
        by_id[definition.id] = definition

    def get(self, kind: DefinitionKind | str, definition_id: str) -> Definition | None:
        return self._definitions[DefinitionKind(kind)].get(definition_id)

    def require(self, kind: DefinitionKind | str, definition_id: str) -> Definition:
        normalized_kind = DefinitionKind(kind)
        definition = self.get(normalized_kind, definition_id)
        if definition is None:
            raise DefinitionReferenceError(
                f"missing definition: {normalized_kind}:{definition_id}"
            )
        return definition

    def all(self, kind: DefinitionKind | str | None = None) -> tuple[Definition, ...]:
        if kind is not None:
            return tuple(self._definitions[DefinitionKind(kind)].values())
        return tuple(
            definition
            for definition_kind in DefinitionKind
            for definition in self._definitions[definition_kind].values()
        )

    def validate_references(self) -> None:
        for definition in self.all():
            for kind, definition_ids in _references(definition):
                for definition_id in definition_ids:
                    if self.get(kind, definition_id) is None:
                        raise DefinitionReferenceError(
                            f"{definition.kind}:{definition.id} references missing "
                            f"{kind}:{definition_id}"
                        )


def _present(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(value for value in values if value)


def _references(
    definition: Definition,
) -> tuple[tuple[DefinitionKind, tuple[str, ...]], ...]:
    if isinstance(definition, CapabilityDefinition):
        return ((DefinitionKind.WORKFLOW, tuple(definition.entrypoints)),)
    if isinstance(definition, AgentDefinition):
        return (
            (DefinitionKind.TOOL, tuple(definition.tools)),
            (
                DefinitionKind.CONTRACT,
                tuple([*definition.accepts, *definition.produces]),
            ),
        )
    if isinstance(definition, ToolDefinition):
        return (
            (
                DefinitionKind.CONTRACT,
                _present(
                    [
                        definition.input_contract,
                        definition.output_contract,
                        definition.error_contract,
                    ]
                ),
            ),
        )
    if isinstance(definition, TaskDefinition):
        return (
            (DefinitionKind.AGENT, (definition.agent,)),
            (
                DefinitionKind.CONTRACT,
                (definition.input_contract, definition.output_contract),
            ),
            (DefinitionKind.TOOL, tuple(definition.tools)),
            (DefinitionKind.RECOVERY, _present([definition.recovery])),
        )
    if isinstance(definition, InteractionDefinition):
        return ((DefinitionKind.CONTRACT, (definition.input_contract,)),)
    if isinstance(definition, OutputDefinition):
        return ((DefinitionKind.CONTRACT, _present([definition.contract])),)
    if isinstance(definition, WorkflowDefinition):
        return (
            (DefinitionKind.TASK, tuple(definition.tasks)),
            (DefinitionKind.RECOVERY, tuple(definition.recovery)),
            (DefinitionKind.INTERACTION, tuple(definition.interactions)),
            (DefinitionKind.OUTPUT, tuple(definition.outputs)),
            (
                DefinitionKind.CONTRACT,
                _present([definition.input_contract, definition.output_contract]),
            ),
        )
    return ()
