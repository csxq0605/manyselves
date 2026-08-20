"""Contract adapters shared by declarative capabilities."""

from copy import deepcopy
from importlib import import_module
from typing import Any, Protocol

from jsonschema import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from jsonschema.validators import validator_for
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from manyselves.kernel.definitions import ContractDefinition


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

    def validate(self, value: Any) -> Any:
        try:
            return self._adapter.validate_python(value)
        except PydanticValidationError as exc:
            raise ContractValidationError(str(exc)) from exc

    def json_schema(self) -> dict[str, Any]:
        return deepcopy(self._adapter.json_schema())

    def is_assignable_to(self, other: ContractAdapter) -> bool:
        return self.json_schema() == other.json_schema()


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
        except JsonSchemaValidationError as exc:
            raise ContractValidationError(exc.message) from exc
        return value

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
