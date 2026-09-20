from pathlib import Path

import pytest

from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    DefinitionReferenceError,
    DefinitionRegistry,
    DuplicateDefinitionError,
    ToolDefinition,
    load_capability,
)


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_registry_rejects_duplicate_kind_and_id() -> None:
    registry = DefinitionRegistry()
    definition = AgentDefinition(
        id="adjuster",
        version="1.0.0",
        description="Adjusts a neutral value",
        instructions="Adjust the value.",
    )
    registry.register(definition)

    with pytest.raises(DuplicateDefinitionError, match="agent:adjuster"):
        registry.register(definition)


def test_registry_reports_missing_typed_reference() -> None:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            id="adjuster",
            version="1.0.0",
            description="Adjusts a neutral value",
            instructions="Adjust the value.",
            tools=["missing-tool"],
        )
    )

    with pytest.raises(
        DefinitionReferenceError,
        match="agent:adjuster references missing tool:missing-tool",
    ):
        registry.validate_references()


def test_load_capability_builds_registry_and_resolves_references(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "capability.yaml",
        """\
id: example
version: 1.0.0
description: Neutral example capability
agents: ./agents
workflows: ./workflows
tasks: ./tasks
contracts: ./contracts
tools: ./tools
recovery: ./recovery
""",
    )
    _write(
        tmp_path,
        "contracts/input.yaml",
        """\
id: adjustment-input
version: 1.0.0
description: Input contract
adapter: json_schema
schema:
  type: object
  properties:
    value: {type: integer}
  required: [value]
""",
    )
    _write(
        tmp_path,
        "contracts/output.json",
        """{
  "id": "adjustment-output",
  "version": "1.0.0",
  "description": "Output contract",
  "adapter": "json_schema",
  "schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
}
""",
    )
    _write(
        tmp_path,
        "tools/increment.yaml",
        """\
id: increment
version: 1.0.0
description: Adds one
implementation: example.tools:increment
input_contract: adjustment-input
output_contract: adjustment-output
side_effect: pure_read
parallel_safe: true
reuse_result: true
""",
    )
    _write(
        tmp_path,
        "agents/adjuster.md",
        """\
---
id: adjuster
version: 1.0.0
description: Adjusts a neutral value
tools: [increment]
accepts: [adjustment-input]
produces: [adjustment-output]
---
Adjust the value.
""",
    )
    _write(
        tmp_path,
        "recovery/default.yaml",
        """\
id: default-recovery
version: 1.0.0
description: Default recovery
rules:
  max_tokens: {action: continue}
""",
    )
    _write(
        tmp_path,
        "tasks/adjust.json",
        """{
  "id": "adjust-value",
  "version": "1.0.0",
  "description": "Adjust a value",
  "agent": "adjuster",
  "objective": "Adjust {value}.",
  "input_contract": "adjustment-input",
  "output_contract": "adjustment-output",
  "tools": ["increment"],
  "recovery": "default-recovery"
}
""",
    )
    _write(
        tmp_path,
        "workflows/adjustment.yaml",
        """\
id: adjustment
version: 1.0.0
description: Neutral adjustment workflow
tasks: [adjust-value]
recovery: [default-recovery]
actions:
  - {id: finish, kind: end_workflow}
""",
    )

    capability, registry = load_capability(tmp_path / "capability.yaml")

    assert capability.id == "example"
    assert registry.require(DefinitionKind.AGENT, "adjuster").instructions == (
        "Adjust the value."
    )
    assert isinstance(
        registry.require(DefinitionKind.TOOL, "increment"),
        ToolDefinition,
    )
    assert len(registry.all()) == 8
