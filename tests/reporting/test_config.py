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
                f"name: {agent_id}",
                "description: evidence",
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


def test_public_loader_rejects_legacy_identity_fields(tmp_path: Path) -> None:
    path = tmp_path / "legacy.md"
    path.write_text(
        "---\nid: legacy-agent\nrole: legacy\n---\n旧身份。",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="extra_forbidden"):
        load_agent_definition(path)


def test_load_nexgent_style_agent_definition(tmp_path: Path) -> None:
    path = tmp_path / "agent.md"
    path.write_text(
        """---
name: module-2.4-specialist
description: 配电设备与元件风险诊断专家
model: inherit
tools: [search_project_evidence, search_reference_library, submit_result]
disallowedTools: [exec]
maxTurns: 12
effort: high
memory: task
background: true
reads: [evidence_items]
writes: [module_drafts]
---
<role_and_perspective>从设备机理与运行条件综合判断。</role_and_perspective>
""",
        encoding="utf-8",
    )

    definition = load_agent_definition(path)

    assert definition.name == "module-2.4-specialist"
    assert definition.disallowed_tools == ["exec"]
    assert definition.max_turns == 12


def test_agent_definition_rejects_tools_that_are_also_disallowed(tmp_path: Path) -> None:
    path = tmp_path / "agent.md"
    path.write_text(
        "---\nname: auditor\ndescription: 审计员\ntools: [exec]\n"
        "disallowedTools: [exec]\n---\n独立核验。",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="tools also listed in disallowedTools"):
        load_agent_definition(path)


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
