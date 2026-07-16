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
        "id: phase-a\nphases:\n  - id: intake\n    mode: pipeline\n    agents: [known, missing]\n",
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
        "id: phase-a\nphases:\n  - id: module\n    mode: parallel\n    agents: [one, two]\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="module_drafts"):
        load_workflow_definition(workflow, agents)


def test_packaged_phase_a_declares_the_complete_vertical_flow() -> None:
    agents, workflow = load_packaged_workflow()

    assert set(agents) == {
        "main-agent",
        "manifest-builder",
        "intake-parser",
        "evidence-normalizer",
        "coverage-evaluator",
        "report-planner",
        "module-2.1-specialist",
        "module-2.2-specialist",
        "module-2.3-specialist",
        "module-2.4-specialist",
        "module-2.5-specialist",
        "evidence-auditor",
        "cross-module-reviewer",
        "chief-editor",
        "citation-builder",
        "docx-renderer",
        "project-delivery",
    }
    assert [(phase.id, phase.mode) for phase in workflow.phases] == [
        ("preparation", "pipeline"),
        ("planning", "pipeline"),
        ("module-pipelines", "parallel"),
        ("module-barrier", "barrier"),
        ("cross-module-review", "pipeline"),
        ("editing-and-delivery", "pipeline"),
    ]


def test_packaged_workflow_has_five_locally_revisable_module_pipelines() -> None:
    agents, workflow = load_packaged_workflow()
    module_phase = next(phase for phase in workflow.phases if phase.id == "module-pipelines")

    assert [pipeline.id for pipeline in module_phase.pipelines] == [
        "module-2.1",
        "module-2.2",
        "module-2.3",
        "module-2.4",
        "module-2.5",
    ]
    for module_id, pipeline in zip(("2.1", "2.2", "2.3", "2.4", "2.5"), module_phase.pipelines):
        assert pipeline.agents == [f"module-{module_id}-specialist", "evidence-auditor"]
        assert pipeline.revision_agent == f"module-{module_id}-specialist"
        assert pipeline.max_revisions == 2
        assert pipeline.revision_agent in agents

    barrier = next(phase for phase in workflow.phases if phase.id == "module-barrier")
    reviewer = next(phase for phase in workflow.phases if phase.id == "cross-module-review")
    assert barrier.needs == ["module-pipelines"]
    assert reviewer.needs == ["module-barrier"]


def test_workflow_rejects_invalid_barrier_and_revision_configuration(tmp_path: Path) -> None:
    agent = load_agent_definition(_write_agent(tmp_path / "known.md", "known"))
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        "id: invalid\nphases:\n"
        "  - id: modules\n    mode: parallel\n    pipelines:\n"
        "      - id: one\n        agents: [known]\n        revisionAgent: missing\n"
        "        maxRevisions: 0\n"
        "  - id: barrier\n    mode: barrier\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError):
        load_workflow_definition(workflow, {agent.id: agent})


def test_packaged_identities_are_complete_scoped_and_corpus_agnostic() -> None:
    agents, _ = load_packaged_workflow()
    human_roles = {
        "main-agent",
        "report-planner",
        "intake-parser",
        "evidence-normalizer",
        "module-2.1-specialist",
        "module-2.2-specialist",
        "module-2.3-specialist",
        "module-2.4-specialist",
        "module-2.5-specialist",
        "evidence-auditor",
        "cross-module-reviewer",
        "chief-editor",
    }
    required_sections = {
        "role_and_perspective",
        "mission",
        "default_posture",
        "owned_decisions",
        "tools_and_loop",
        "collaboration",
        "completion_standard",
        "deliverables",
    }
    for agent_id in human_roles:
        identity = agents[agent_id]
        assert required_sections <= {
            section for section in required_sections if f"<{section}>" in identity.instructions
        }

    expert_tools = set(agents["module-2.4-specialist"].tools)
    assert {
        "search_project_evidence",
        "search_reference_library",
        "web_search",
        "query_peer",
    } <= expert_tools
    assert "web_search" not in agents["intake-parser"].tools
    assert "request_revision" in agents["evidence-auditor"].tools
    assert "request_revision" in agents["cross-module-reviewer"].tools
    assert "web_search" not in agents["chief-editor"].tools
    assert agents["docx-renderer"].tools == []

    combined = "\n".join(agent.instructions for agent in agents.values())
    forbidden = (
        "配电安全报告工具V2-交接",
        "现状—结论—风险—建议",
        "事实—风险—建议",
    )
    assert not any(term in combined for term in forbidden)
