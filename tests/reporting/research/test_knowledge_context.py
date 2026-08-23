import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import SpecialTopicPlan
from manyselves.core.reporting.research.knowledge_context import KnowledgeContextBuilder


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_context_manifest_records_project_and_global_sources(tmp_path: Path) -> None:
    project = tmp_path / "project"
    global_root = tmp_path / "global"
    _write(project / "Knowledge/project.md", "project reference")
    _write(global_root / "global.md", "global reference")

    builder = KnowledgeContextBuilder(project, "run-1", global_root=global_root)
    documents = builder.freeze_sources()
    manifest = json.loads(
        (project / "Work/runs/run-1/context-manifests/knowledge-sources.json").read_text(
            encoding="utf-8"
        )
    )

    assert {document.namespace for document in documents} == {"project", "global"}
    assert {item["namespace"] for item in manifest["sources"]} == {"project", "global"}
    assert {item["logicalPath"] for item in manifest["sources"]} == {
        "Knowledge/project.md",
        "GlobalKnowledge/global.md",
    }
    assert all(len(item["sha256"]) == 64 for item in manifest["sources"])


def test_run_source_freeze_ignores_later_global_upload_across_builders(tmp_path: Path) -> None:
    project = tmp_path / "project"
    global_root = tmp_path / "global"
    _write(global_root / "initial.md", "initial reference")
    builder = KnowledgeContextBuilder(project, "run-1", global_root=global_root)

    initial = builder.freeze_sources()
    _write(global_root / "late.md", "late reference")
    same_builder = builder.freeze_sources()
    restarted_builder = KnowledgeContextBuilder(
        project, "run-1", global_root=global_root
    ).freeze_sources()

    assert [document.relative_path for document in initial] == [
        "GlobalKnowledge/initial.md"
    ]
    assert same_builder == initial
    assert restarted_builder == initial


def test_knowledge_context_is_taxonomy_aligned_and_registers_local_sources(tmp_path) -> None:
    knowledge = tmp_path / "Knowledge/rules.md"
    knowledge.parent.mkdir(parents=True)
    knowledge.write_text(
        "# 配网自动化、备用电源自动切换（可能性及功能验证）\n"
        "应核验切换逻辑、切换时间、闭锁条件和定期试验记录。\n"
        "# 报告质量\n风险分析需要说明根因、影响和恶化条件。\n",
        encoding="utf-8",
    )

    builder = KnowledgeContextBuilder(tmp_path, "run-knowledge")
    module = builder.build_module("2.1")
    quality = builder.build_quality()

    assert "2.1.3 配网自动化、备用电源自动切换（可能性及功能验证）" in module.text
    assert "切换逻辑、切换时间" in module.text
    assert "R-001" in module.text
    assert "风险分析需要说明根因" in quality.text
    assert (tmp_path / module.path).is_file()
    ledger = json.loads(
        (tmp_path / "Work/runs/run-knowledge/ledgers/sources.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["kind"] for item in ledger] == ["local_reference"]


def test_special_topic_knowledge_uses_dynamic_titles_and_keeps_world_knowledge_boundary(
    tmp_path,
) -> None:
    knowledge = tmp_path / "Knowledge/并机参考.md"
    knowledge.parent.mkdir(parents=True)
    knowledge.write_text(
        "# 柴油发电机并机条件\n"
        "并机方案应校核同期条件、保护配合、闭锁逻辑和联合试验记录。",
        encoding="utf-8",
    )
    plan = SpecialTopicPlan(
        source_ref="Inputs/专项问题分析.md",
        source_sha256="0" * 64,
        sections=[
            {
                "section_id": "4.1",
                "title": "柴油发电机并机条件",
                "requirement": "说明适用边界、联锁条件和验收方法。",
            }
        ],
    )

    context = KnowledgeContextBuilder(tmp_path, "run-special").build_special_topics(
        plan
    )

    assert "4.1 柴油发电机并机条件" in context.text
    assert "同期条件、保护配合" in context.text
    assert "可使用模型已有专业知识" in context.text
    assert "不是客户现场事实" in context.text
    assert context.source_ids == ("R-001",)


def test_module_knowledge_deduplicates_snippets_and_excludes_report_rules(
    tmp_path,
) -> None:
    knowledge = tmp_path / "Knowledge"
    knowledge.mkdir(parents=True)
    professional = (
        "# 2.1.3 配网自动化、备用电源自动切换（可能性及功能验证）\n"
        "自动切换应核验逻辑、闭锁条件、动作时序和定期试验记录。"
    )
    (knowledge / "professional-a.md").write_text(professional, encoding="utf-8")
    (knowledge / "professional-b.md").write_text(professional, encoding="utf-8")
    (knowledge / "报告模板.md").write_text(
        "报告模板要求在 2.1.3 配网自动化、备用电源自动切换（可能性及功能验证）章节使用固定句式。",
        encoding="utf-8",
    )

    context = KnowledgeContextBuilder(tmp_path, "run-dedupe").build_module("2.1")

    assert context.text.count("自动切换应核验逻辑、闭锁条件") == 1
    assert "报告模板要求" not in context.text
    assert "报告模板.md" not in context.text


def test_large_module_knowledge_preserves_every_submodule_with_bounded_quota(
    tmp_path,
) -> None:
    knowledge = tmp_path / "Knowledge"
    knowledge.mkdir(parents=True)
    module = REPORT_TAXONOMY["2.4"]
    content = "\n\n".join(
        (
            f"# {submodule_id} {submodule.title}\n"
            f"{submodule.title} 的专业核查需记录对象、状态、机理、风险后果和验证条件。"
            + f" 专业补充说明{index}。" * 30
        )
        for index, (submodule_id, submodule) in enumerate(
            module.submodules.items(),
            start=1,
        )
    )
    (knowledge / "equipment-reference.md").write_text(content, encoding="utf-8")

    context = KnowledgeContextBuilder(tmp_path, "run-large").build_module("2.4")

    assert len(context.text) <= KnowledgeContextBuilder.MAX_MODULE_CHARS
    for submodule_id, submodule in module.submodules.items():
        assert f"## {submodule_id} {submodule.title}" in context.text
    assert "## 2.4.4" in context.text


def test_knowledge_snapshot_rejects_hash_consistent_retired_history_token(
    tmp_path,
) -> None:
    knowledge = tmp_path / "Knowledge/poisoned.md"
    knowledge.parent.mkdir(parents=True)
    knowledge.write_text(
        "<persisted_result_part sha256=" + "a" * 64 + " characters=100>",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="retired internal history token",
    ) as exc_info:
        KnowledgeContextBuilder(tmp_path, "run-poisoned").build_module("2.1")

    assert "persisted_result_part" not in str(exc_info.value)
