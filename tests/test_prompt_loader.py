"""Tests for prompt loader."""

import tempfile
from pathlib import Path

import pytest

from manyselves.core.prompts.loader import PromptLoader


@pytest.fixture
def agents_dir():
    d = Path(tempfile.mkdtemp())
    (d / "main_agent.md").write_text(
        "# Main Agent\n\nYou are the main agent.\n\n## Core Rules\n\nCoordinate sub-agents.\n",
        encoding="utf-8",
    )
    return d


def test_load_prompt_main(agents_dir):
    loader = PromptLoader(agents_dir=agents_dir)
    result = loader.load_prompt("main")
    assert "main agent" in result


def test_cache_hits(agents_dir):
    loader = PromptLoader(agents_dir=agents_dir)
    first = loader.load_prompt("main")
    second = loader.load_prompt("main")
    assert first == second
    assert first is second  # same object, cached


def test_reload_clears_cache(agents_dir):
    loader = PromptLoader(agents_dir=agents_dir)
    loader.load_prompt("main")
    assert "main" in loader._cache

    loader.reload()
    assert "main" not in loader._cache


def test_fallback_for_missing_file(agents_dir):
    loader = PromptLoader(agents_dir=agents_dir)
    result = loader.load_prompt("nonexistent_agent")
    assert "nonexistent_agent" in result


def test_fallback_built_in_types(agents_dir):
    loader = PromptLoader(agents_dir=agents_dir)
    assert len(loader.load_prompt("main")) > 0


def test_load_shared_context_available(agents_dir):
    d = agents_dir
    (d / "Common.md").write_text("## Shared\n\nCommon todo policy.", encoding="utf-8")
    loader = PromptLoader(agents_dir=d)
    result = loader.load_shared_context()
    assert "Common todo policy" in result


def test_load_shared_context_missing(agents_dir):
    loader = PromptLoader(agents_dir=agents_dir)
    result = loader.load_shared_context()
    assert result is None


def test_get_filename_mapping(agents_dir):
    loader = PromptLoader(agents_dir=agents_dir)
    assert loader._get_filename("main") == "main_agent.md"


def test_get_filename_unknown_type(agents_dir):
    loader = PromptLoader(agents_dir=agents_dir)
    assert loader._get_filename("custom") == "custom_agent.md"


def test_get_filename_normalizes_hyphens_for_task_roles(agents_dir):
    loader = PromptLoader(agents_dir=agents_dir)
    assert loader._get_filename("evidence-auditor") == "evidence_auditor_agent.md"


def test_packaged_main_prompt_routes_distribution_reports_through_workflow_tool():
    prompt = PromptLoader().load_prompt("main")

    assert "run_reporting_workflow" in prompt
    assert "resume_reporting_workflow" in prompt
    assert "当前 run" in prompt
    assert "历史 Outputs" in prompt
    assert "配电报告" in prompt
    assert "automated physics experiment" not in prompt.lower()
