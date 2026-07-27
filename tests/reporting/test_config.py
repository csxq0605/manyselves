from pathlib import Path

import pytest

from manyselves.core.reporting.config import (
    ConfigurationError,
    load_agent_definition,
    load_packaged_agents,
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
    assert definition.max_tokens is None


def test_agent_definition_rejects_tools_that_are_also_disallowed(tmp_path: Path) -> None:
    path = tmp_path / "agent.md"
    path.write_text(
        "---\nname: auditor\ndescription: 审计员\ntools: [exec]\n"
        "disallowedTools: [exec]\n---\n独立核验。",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="tools also listed in disallowedTools"):
        load_agent_definition(path)


def test_packaged_agent_set_is_complete() -> None:
    agents = load_packaged_agents()

    assert set(agents) == {
        "main-agent",
        "template-distiller",
        "manifest-builder",
        "intake-parser",
        "evidence-normalizer",
        "coverage-evaluator",
        "module-2.1-specialist",
        "module-2.2-specialist",
        "module-2.3-specialist",
        "module-2.4-specialist",
        "module-2.5-specialist",
        "evidence-auditor",
        "cross-module-reviewer",
        "chief-editor",
        "chief-editor-auditor",
        "citation-builder",
        "docx-renderer",
        "project-delivery",
        "product-skill-maintainer",
    }
    assert agents["chief-editor"].max_tokens == 32768
    assert agents["chief-editor"].max_turns == 28
    assert agents["module-2.4-specialist"].max_tokens == 12288
    assert agents["cross-module-reviewer"].max_tokens == 32768
    assert agents["cross-module-reviewer"].max_turns == 28


def test_packaged_identities_are_complete_scoped_and_corpus_agnostic() -> None:
    agents = load_packaged_agents()
    human_roles = {
        "main-agent",
        "template-distiller",
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
        "chief-editor-auditor",
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
    assert "request_revision" not in agents["evidence-auditor"].tools
    assert "request_revision" not in agents["cross-module-reviewer"].tools
    assert "web_search" not in agents["chief-editor"].tools
    assert agents["docx-renderer"].tools == []

    combined = "\n".join(agent.instructions for agent in agents.values())
    forbidden = (
        "配电安全报告工具V2-交接",
        "现状—结论—风险—建议",
        "事实—风险—建议",
    )
    assert not any(term in combined for term in forbidden)
