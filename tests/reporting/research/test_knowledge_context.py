import json

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
