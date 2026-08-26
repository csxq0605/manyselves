"""Tests for prompt loader."""

import tempfile
from pathlib import Path

import pytest

from manyselves.runtime.prompts.loader import PromptLoader


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
    assert "reporting" not in result.lower()
    assert "power-distribution" not in result.lower()


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


def test_packaged_main_prompt_preserves_distribution_demo_entrypoint():
    prompt = PromptLoader().load_prompt("main")

    assert "配电安全服务 Demo" in prompt
    assert "run_reporting_workflow" in prompt
    assert "不需要先列出工作流或读取 Schema" in prompt
    assert "当前尚未完成" in prompt
    assert "不要承诺完成后主动汇报" in prompt
    assert "Kernel 保持无状态且业务无关" in prompt
