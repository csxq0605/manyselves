"""Knowledge packets must not disguise clipped standards as complete rules."""

from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    SpecialTopicPlan,
    SpecialTopicSectionRequirement,
)
from manyselves.capabilities.distribution_reporting.runtime.research.knowledge_context import (
    KnowledgeContextBuilder,
)
from manyselves.capabilities.distribution_reporting.runtime.research.reference_library import (
    ReferenceDocument,
)


def _builder(tmp_path, texts):
    builder = KnowledgeContextBuilder(tmp_path, "knowledge-fidelity")
    builder._documents = tuple(
        ReferenceDocument(f"规程{index}", f"Knowledge/rule-{index}.md", text, str(index))
        for index, text in enumerate(texts)
    )
    return builder


def test_module_delivers_complete_selected_document_once_including_late_exception(tmp_path):
    text = "# 适用范围\n仅适用于工况甲。\n# 2.4.4\n" + "测量方法。" * 250 + "\n例外：工况乙不适用。"
    builder = _builder(tmp_path, [text])
    packet = builder.build_module("2.4")
    assert text in packet.text
    assert packet.text.count("例外：工况乙不适用。") == 1
    assert len(packet.source_ids) == 1
    assert "完整原文" in packet.text


def test_equal_excerpt_does_not_merge_different_authorities(tmp_path):
    prefix = "# 2.4.4\n" + "相同前文。" * 250
    packet = _builder(tmp_path, [prefix + "\n甲仅适用于A。", prefix + "\n乙仅适用于B。"]).build_module("2.4")
    assert len(packet.source_ids) == 2
    assert "甲仅适用于A。" in packet.text and "乙仅适用于B。" in packet.text


def test_oversize_document_has_explicit_unexpanded_status_and_complete_source(tmp_path):
    text = "# 2.4.4\n" + "规定。" * 8000 + "\n尾部例外。"
    builder = _builder(tmp_path, [text])
    packet = builder.build_module("2.4")
    assert len(packet.source_ids) == 1
    assert "原文未展开" in packet.text and "open_reference" in packet.text
    assert "Knowledge/rule-0.md" in packet.text
    assert "规定。" not in packet.text  # no plausible-looking partial rule
    ref = builder.ledger.content_ref(packet.source_ids[0])
    assert ref is not None and ref.as_posix() in packet.text
    assert (tmp_path / ref).read_text(encoding="utf-8") == text
    assert len(packet.text) <= builder.MAX_MODULE_CHARS


def test_quality_packet_preserves_complete_selected_rule(tmp_path):
    text = "# 报告质量\n" + "检查方法。" * 250 + "\n例外情况必须列明。"
    packet = _builder(tmp_path, [text]).build_quality()
    assert text in packet.text


def test_special_topic_never_truncates_user_requirement_to_fit_knowledge_budget(tmp_path):
    requirement = "需要核实工况。" * 2100 + "尾部用户要求。"
    plan = SpecialTopicPlan(
        source_ref=Path("Inputs/topics.md"), source_sha256="a" * 64,
        sections=[SpecialTopicSectionRequirement(section_id="4.1", title="检测", requirement=requirement)],
    )
    packet = _builder(tmp_path, ["检测\n规则原文。" * 1000]).build_special_topics(plan)
    assert requirement in packet.text
    assert "原文未展开" in packet.text
    assert len(packet.source_ids) == 1


@pytest.mark.asyncio
async def test_unexpanded_source_opens_through_real_reference_tool(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.research_tools import (
        OpenReferenceTool,
    )

    source = tmp_path / "Knowledge" / "thermal.md"
    source.parent.mkdir()
    text = "# 2.4.4\n" + "测量条件。" * 4000 + "\n尾部适用例外。"
    source.write_text(text, encoding="utf-8")
    builder = KnowledgeContextBuilder(tmp_path, "reference-tool")
    packet = builder.build_module("2.4")
    assert "原文未展开" in packet.text
    result = await OpenReferenceTool(builder.library, builder.ledger)(locator="Knowledge/thermal.md")
    assert result["source_id"] in packet.source_ids
    assert result["text"] == text


def test_one_document_serving_multiple_sections_is_inlined_once(tmp_path):
    text = "# 2.4.1.1\n工况说明。\n# 2.4.4\n另一个主题的适用条件。"
    packet = _builder(tmp_path, [text]).build_module("2.4")
    assert packet.text.count(text) == 1
    assert packet.text.count("候选来源：R-001") >= 2
