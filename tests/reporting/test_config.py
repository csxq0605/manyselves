from pathlib import Path

import pytest

from autoreport.core.reporting.config import (
    ConfigurationError,
    load_agent_definition,
    load_agent_definitions,
    load_packaged_workflow,
    load_workflow_definition,
)


def _write_agent(path: Path, agent_id: str, *, writes: str = "evidence_items") -> Path:
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {agent_id}",
                "role: evidence",
                "reads: [parsed_artifacts]",
                f"writes: [{writes}]",
                "tools: [read]",
                "---",
                "只从项目材料生成可追溯证据。",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_loads_agent_frontmatter_and_instruction_body(tmp_path: Path) -> None:
    path = _write_agent(tmp_path / "normalizer.md", "evidence-normalizer")

    agent = load_agent_definition(path)

    assert agent.id == "evidence-normalizer"
    assert agent.writes == ["evidence_items"]
    assert "可追溯证据" in agent.instructions


def test_workflow_rejects_unknown_agent(tmp_path: Path) -> None:
    agent = load_agent_definition(_write_agent(tmp_path / "known.md", "known"))
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        "id: phase-a\nphases:\n  - id: intake\n    mode: pipeline\n"
        "    agents: [known, missing]\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="missing"):
        load_workflow_definition(workflow, {agent.id: agent})


def test_parallel_phase_rejects_conflicting_writes(tmp_path: Path) -> None:
    _write_agent(tmp_path / "one.md", "one", writes="module_drafts")
    _write_agent(tmp_path / "two.md", "two", writes="module_drafts")
    agents = load_agent_definitions(tmp_path)
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        "id: phase-a\nphases:\n  - id: module\n    mode: parallel\n"
        "    agents: [one, two]\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="module_drafts"):
        load_workflow_definition(workflow, agents)


def test_packaged_phase_a_declares_the_complete_vertical_flow() -> None:
    agents, workflow = load_packaged_workflow()

    assert set(agents) == {
        "manifest-builder",
        "artifact-parser",
        "evidence-normalizer",
        "coverage-evaluator",
        "report-planner",
        "module-worker",
        "evidence-auditor",
        "revision-router",
        "project-delivery",
    }
    assert [(phase.id, phase.mode) for phase in workflow.phases] == [
        ("intake", "pipeline"),
        ("coverage", "pipeline"),
        ("module", "parallel"),
        ("quality", "pipeline"),
    ]
