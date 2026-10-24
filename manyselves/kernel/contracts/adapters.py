"""Contract adapters shared by declarative capabilities."""

from copy import deepcopy
from importlib import import_module
from typing import Any, Protocol

from jsonschema import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from jsonschema.validators import validator_for
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from manyselves.kernel.contracts.relocation import relocate
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionKind,
    DefinitionRegistry,
)


class ContractDefinitionError(ValueError):
    """Raised when a contract implementation reference cannot be built."""


class ContractValidationError(ValueError):
    """Raised when a value does not satisfy its declared contract."""


class ContractAdapter(Protocol):
    """Uniform contract interface consumed by later runtime work packages."""

    def validate(self, value: Any) -> Any: ...

    def json_schema(self) -> dict[str, Any]: ...

    def is_assignable_to(self, other: "ContractAdapter") -> bool: ...


class PydanticContractAdapter:
    """Contract adapter backed by an importable Pydantic-compatible type."""

    def __init__(self, model: Any) -> None:
        self._adapter = TypeAdapter(model)
        self._json_schema: dict[str, Any] | None = None

    def validate(self, value: Any) -> Any:
        try:
            return self._adapter.validate_python(value)
        except PydanticValidationError as exc:
            first = exc
        # A field submitted at the wrong level goes back where the contract
        # declares it, when the contract makes that place certain.
        moved, notes = relocate(value, self._schema())
        if moved != value:
            try:
                return self._adapter.validate_python(moved)
            except PydanticValidationError as exc:
                first, value = exc, moved
        raise ContractValidationError(_compact_errors(first, value, notes)) from first

    def _schema(self) -> dict[str, Any]:
        if self._json_schema is None:
            try:
                self._json_schema = self._adapter.json_schema()
            except Exception:  # a type without a JSON schema relocates nothing
                self._json_schema = {}
        return self._json_schema

    def json_schema(self) -> dict[str, Any]:
        return deepcopy(self._adapter.json_schema())

    def is_assignable_to(self, other: ContractAdapter) -> bool:
        return self.json_schema() == other.json_schema()


def _item_label(value: Any, loc: tuple[Any, ...]) -> str:
    """The location, naming each list item by its own ``id`` when it has one."""

    parts: list[str] = []
    current = value
    for key in loc:
        if isinstance(key, int) and isinstance(current, list) and 0 <= key < len(current):
            current = current[key]
            item_id = current.get("id") if isinstance(current, dict) else None
            parts.append(f"[{key}]" + (f"（{item_id}）" if isinstance(item_id, str) else ""))
            continue
        current = current.get(key) if isinstance(current, dict) else None
        parts.append(("." if parts else "") + str(key))
    return "".join(parts) or "(根)"


def _compact_errors(
    exc: PydanticValidationError, value: Any, notes: list[tuple[str, str]] | None = None
) -> str:
    """One line per error with its location, without echoing the submitted values.

    A field the contract places elsewhere is named once, with where it goes;
    the errors inside it at the wrong place are left out, since they describe
    the wrong parent's entries and would lead the correction astray.
    """

    notes = notes or []
    misplaced = [at for at, _line in notes]
    lines = [line for _at, line in notes]
    for error in exc.errors(include_url=False, include_input=False):
        dotted = ".".join(str(part) for part in error["loc"])
        if any(dotted == at or dotted.startswith(at + ".") for at in misplaced):
            continue
        lines.append(f"{_item_label(value, tuple(error['loc']))}: {error['msg']}")
    return f"{len(lines)} 处不符合：\n" + "\n".join(lines)


class JsonSchemaContractAdapter:
    """Contract adapter backed by the declared JSON Schema dialect."""

    def __init__(self, schema: dict[str, Any]) -> None:
        self._schema = deepcopy(schema)
        validator_type = validator_for(self._schema)
        try:
            validator_type.check_schema(self._schema)
        except SchemaError as exc:
            raise ContractDefinitionError(str(exc)) from exc
        self._validator = validator_type(self._schema)

    def validate(self, value: Any) -> Any:
        try:
            self._validator.validate(value)
            return value
        except JsonSchemaValidationError as exc:
            first = exc
        moved, notes = relocate(value, self._schema)
        if moved != value:
            try:
                self._validator.validate(moved)
                return moved
            except JsonSchemaValidationError as exc:
                first = exc
        message = "\n".join([line for _at, line in notes] + [first.message])
        raise ContractValidationError(message) from first

    def json_schema(self) -> dict[str, Any]:
        return deepcopy(self._schema)

    def is_assignable_to(self, other: ContractAdapter) -> bool:
        return self.json_schema() == other.json_schema()


def _import_reference(reference: str) -> Any:
    module_name, separator, attribute_path = reference.partition(":")
    if not separator or not module_name or not attribute_path:
        raise ContractDefinitionError(
            "Pydantic model reference must use 'module:attribute'"
        )
    try:
        value: Any = import_module(module_name)
        for part in attribute_path.split("."):
            value = getattr(value, part)
    except (ImportError, AttributeError) as exc:
        raise ContractDefinitionError(
            f"cannot import Pydantic model: {reference}"
        ) from exc
    return value


def build_contract_adapter(definition: ContractDefinition) -> ContractAdapter:
    """Build the adapter selected by one validated contract definition."""

    if definition.adapter == "pydantic":
        assert definition.model is not None
        return PydanticContractAdapter(_import_reference(definition.model))
    assert definition.schema_ is not None
    return JsonSchemaContractAdapter(definition.schema_)


def build_contract_catalog(
    definitions: DefinitionRegistry,
) -> dict[str, ContractAdapter]:
    """Build every file-declared Contract adapter in one Registry."""

    return {
        definition.id: build_contract_adapter(definition)
        for definition in definitions.all(DefinitionKind.CONTRACT)
        if isinstance(definition, ContractDefinition)
    }
