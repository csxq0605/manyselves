from pathlib import Path

import pytest

from manyselves.kernel.definitions import (
    AgentDefinition,
    CapabilityDefinition,
    ContractDefinition,
    DefinitionKind,
    DefinitionLoadError,
    GateDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
    ToolDefinition,
    WorkflowDefinition,
    load_definition,
)


def test_definition_models_cover_the_wp01_vocabulary() -> None:
    definitions = [
        CapabilityDefinition(
            id="example",
            version="1.0.0",
            description="Example capability",
            agents="./agents",
            workflows="./workflows",
            tasks="./tasks",
            contracts="./contracts",
            tools="./tools",
            gates="./gates",
            recovery="./recovery",
        ),
        AgentDefinition(
            id="adjuster",
            version="1.0.0",
            description="Adjusts a neutral value",
            instructions="Return the adjusted value.",
            tools=["increment"],
            accepts=["adjustment-input"],
            produces=["adjustment-output"],
        ),
        ToolDefinition(
            id="increment",
            version="1.0.0",
            description="Adds one",
            implementation="example.tools:increment",
            input_contract="adjustment-input",
            output_contract="adjustment-output",
            side_effect="pure_read",
            parallel_safe=True,
            reuse_result=True,
        ),
        ContractDefinition(
            id="adjustment-input",
            version="1.0.0",
            description="Neutral input",
            adapter="json_schema",
            schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
        ),
        TaskDefinition(
            id="adjust-value",
            version="1.0.0",
            description="Adjust a value",
            agent="adjuster",
            objective="Adjust {value}.",
            input_contract="adjustment-input",
            output_contract="adjustment-output",
            recovery="default-recovery",
        ),
        GateDefinition(
            id="positive",
            version="1.0.0",
            description="Checks a positive result",
            contract="adjustment-output",
            expression="result.value > 0",
            on_pass="finish",
            on_fail="adjust-value",
        ),
        RecoveryPolicyDefinition(
            id="default-recovery",
            version="1.0.0",
            description="Neutral recovery policy",
            rules={"max_tokens": {"action": "continue"}},
        ),
        WorkflowDefinition(
            id="adjustment",
            version="1.0.0",
            description="Neutral workflow",
            tasks=["adjust-value"],
            gates=["positive"],
            recovery=["default-recovery"],
            actions=[{"id": "finish", "kind": "end_workflow"}],
        ),
    ]

    assert [definition.kind for definition in definitions] == list(DefinitionKind)


@pytest.mark.parametrize(
    ("suffix", "content", "expected_type", "expected_id"),
    [
        (
            ".yaml",
            """\
kind: tool
id: increment
version: 1.0.0
description: Adds one
implementation: example.tools:increment
input_contract: adjustment-input
output_contract: adjustment-output
""",
            ToolDefinition,
            "increment",
        ),
        (
            ".json",
            """{
  "kind": "task",
  "id": "adjust-value",
  "version": "1.0.0",
  "description": "Adjust a value",
  "agent": "adjuster",
  "objective": "Adjust {value}.",
  "input_contract": "adjustment-input",
  "output_contract": "adjustment-output"
}
""",
            TaskDefinition,
            "adjust-value",
        ),
        (
            ".md",
            """\
---
kind: agent
id: adjuster
version: 1.0.0
description: Adjusts a neutral value
tools:
  - increment
accepts:
  - adjustment-input
produces:
  - adjustment-output
---
Return the adjusted value.
""",
            AgentDefinition,
            "adjuster",
        ),
    ],
)
def test_load_definition_supports_yaml_json_and_markdown_frontmatter(
    tmp_path: Path,
    suffix: str,
    content: str,
    expected_type: type,
    expected_id: str,
) -> None:
    path = tmp_path / f"definition{suffix}"
    path.write_text(content, encoding="utf-8")

    definition = load_definition(path)

    assert isinstance(definition, expected_type)
    assert definition.id == expected_id
    if isinstance(definition, AgentDefinition):
        assert definition.instructions == "Return the adjusted value."


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("broken.yaml", "kind: [", "invalid YAML"),
        ("list.yaml", "- one\n- two\n", "top level must be an object"),
        ("missing.md", "No frontmatter", "missing YAML frontmatter"),
        ("unterminated.md", "---\nkind: agent\n", "unterminated YAML frontmatter"),
    ],
)
def test_load_definition_reports_parse_boundaries(
    tmp_path: Path,
    name: str,
    content: str,
    message: str,
) -> None:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")

    with pytest.raises(DefinitionLoadError, match=message):
        load_definition(path)
