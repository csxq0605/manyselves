import json

from manyselves.core.reporting.models import SpecialTopicPlan
from manyselves.core.reporting.research.knowledge_context import KnowledgeContextBuilder


def test_knowledge_context_is_taxonomy_aligned_and_registers_local_sources(tmp_path) -> None:
    knowledge = tmp_path / "Knowledge/rules.md"
    knowledge.parent.mkdir(parents=True)
    knowledge.write_text(
        "# 配网自动化、备用电源自动切换\n"
        "应核验切换逻辑、切换时间、闭锁条件和定期试验记录。\n"
        "# 报告质量\n风险分析需要说明根因、影响和恶化条件。\n",
        encoding="utf-8",
    )

    builder = KnowledgeContextBuilder(tmp_path, "run-knowledge")
    module = builder.build_module("2.1")
    quality = builder.build_quality()

    assert "2.1.3 配网自动化、备用电源自动切换" in module.text
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
