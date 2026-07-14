from pathlib import Path

import pytest

from pds_report.workflow.config import (
    ConfigurationError,
    load_agent_definition,
    load_agent_definitions,
    load_workflow,
)

TEST_ROOT = Path(".test-projects/config")


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def agent_markdown(agent_id: str = "manifest-builder") -> str:
    return f"""---
id: {agent_id}
role: intake
reads: [report_request]
writes: [project_manifest]
tools: [scan_project]
---

扫描项目资料并建立清单。
"""


def test_loads_markdown_frontmatter_and_body() -> None:
    path = write(TEST_ROOT / "manifest-builder.md", agent_markdown())

    agent = load_agent_definition(path)

    assert agent.id == "manifest-builder"
    assert agent.writes == ["project_manifest"]
    assert "扫描项目资料" in agent.instructions


def test_agent_requires_explicit_read_and_write_contracts() -> None:
    path = write(
        TEST_ROOT / "missing-contract.md",
        """---
id: broken
role: intake
tools: []
---
Broken.
""",
    )

    with pytest.raises(ConfigurationError, match="reads.*writes"):
        load_agent_definition(path)


def test_agent_rejects_unknown_carrier() -> None:
    path = write(
        TEST_ROOT / "unknown-carrier.md",
        agent_markdown().replace("project_manifest", "mystery_state"),
    )

    with pytest.raises(ConfigurationError, match="mystery_state"):
        load_agent_definition(path)


def test_agent_directory_rejects_duplicate_ids() -> None:
    directory = TEST_ROOT / "duplicate-agents"
    write(directory / "one.md", agent_markdown("same"))
    write(directory / "two.md", agent_markdown("same"))

    with pytest.raises(ConfigurationError, match="duplicate agent id: same"):
        load_agent_definitions(directory)


def test_workflow_rejects_unknown_agent() -> None:
    agent_path = write(TEST_ROOT / "known.md", agent_markdown("known"))
    workflow_path = write(
        TEST_ROOT / "unknown-agent.yml",
        """id: invalid
phases:
  - id: intake
    mode: pipeline
    agents: [known, unknown-agent]
""",
    )

    with pytest.raises(ConfigurationError, match="unknown-agent"):
        load_workflow(workflow_path, {"known": load_agent_definition(agent_path)})


def test_workflow_rejects_unsupported_mode() -> None:
    agent_path = write(TEST_ROOT / "known.md", agent_markdown("known"))
    workflow_path = write(
        TEST_ROOT / "invalid-mode.yml",
        """id: invalid
phases:
  - id: intake
    mode: fanout
    agents: [known]
""",
    )

    with pytest.raises(ConfigurationError, match="fanout"):
        load_workflow(workflow_path, {"known": load_agent_definition(agent_path)})


def test_workflow_rejects_phase_cycle() -> None:
    agent_path = write(TEST_ROOT / "known.md", agent_markdown("known"))
    workflow_path = write(
        TEST_ROOT / "cycle.yml",
        """id: invalid
phases:
  - id: first
    mode: pipeline
    agents: [known]
    needs: [second]
  - id: second
    mode: parallel
    agents: [known]
    needs: [first]
""",
    )

    with pytest.raises(ConfigurationError, match="cycle"):
        load_workflow(workflow_path, {"known": load_agent_definition(agent_path)})


def test_workflow_defaults_each_phase_to_previous_phase() -> None:
    agent_path = write(TEST_ROOT / "known.md", agent_markdown("known"))
    workflow_path = write(
        TEST_ROOT / "ordered.yml",
        """id: valid
phases:
  - id: intake
    mode: pipeline
    agents: [known]
  - id: quality
    mode: parallel
    agents: [known]
""",
    )

    workflow = load_workflow(
        workflow_path,
        {"known": load_agent_definition(agent_path)},
    )

    assert workflow.phases[0].needs == []
    assert workflow.phases[1].needs == ["intake"]
