import ast
import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

DOMAIN_ROOT = "manyselves.capabilities.distribution_reporting.domain"
DOMAIN_MODULES = {
    "claim_ledger": f"{DOMAIN_ROOT}.claim_ledger",
    "coverage": f"{DOMAIN_ROOT}.coverage",
    "revision_diff": f"{DOMAIN_ROOT}.revision_diff",
    "cross_specialization": f"{DOMAIN_ROOT}.cross_specialization",
    "final_specialization": f"{DOMAIN_ROOT}.final_specialization",
}


def test_pure_domain_modules_import_without_core_reporting() -> None:
    imports = [
        f"import {module_name} as {alias}"
        for alias, module_name in DOMAIN_MODULES.items()
    ]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    *imports,
                    "print(json.dumps({",
                    "    'modules': sorted((",
                    *(
                        f"        {alias}.__name__,"
                        for alias in DOMAIN_MODULES
                    ),
                    "    )),",
                    "    'core_reporting': sorted(",
                    "        name for name in sys.modules",
                    "        if name.startswith('manyselves.core.reporting')",
                    "    ),",
                    "}))",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "modules": sorted(DOMAIN_MODULES.values()),
        "core_reporting": [],
    }


def test_pure_domain_symbols_are_physically_capability_owned() -> None:
    expected_symbols = {
        "claim_ledger": ("CitationBindingError", "ClaimLedger", "claim_citation_marker"),
        "coverage": ("evaluate_coverage",),
        "revision_diff": ("build_revision_diff",),
        "cross_specialization": (
            "CrossLaneSpecialization",
            "cross_lane_specialization",
        ),
        "final_specialization": (
            "FinalLaneSpecialization",
            "final_lane_specialization",
        ),
    }

    for alias, module_name in DOMAIN_MODULES.items():
        module = import_module(module_name)
        for symbol_name in expected_symbols[alias]:
            assert getattr(module, symbol_name).__module__ == module_name

        source = Path(module.__file__).read_text(encoding="utf-8")
        imports = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        assert all(
            not any(
                name.name.startswith("manyselves.core.reporting")
                for name in node.names
            )
            if isinstance(node, ast.Import)
            else not (node.module or "").startswith("manyselves.core.reporting")
            for node in imports
        )
        assert find_spec(f"manyselves.core.reporting.{alias}") is None


def test_pure_domain_representative_outputs_remain_exact() -> None:
    claim_ledger = import_module(DOMAIN_MODULES["claim_ledger"])
    coverage = import_module(DOMAIN_MODULES["coverage"])
    revision_diff = import_module(DOMAIN_MODULES["revision_diff"])
    cross = import_module(DOMAIN_MODULES["cross_specialization"])
    final = import_module(DOMAIN_MODULES["final_specialization"])
    agentic = import_module(
        "manyselves.capabilities.distribution_reporting.runtime.models.agentic"
    )
    reporting = import_module(
        "manyselves.capabilities.distribution_reporting.runtime.models.reporting"
    )
    taxonomy = import_module(f"{DOMAIN_ROOT}.taxonomy")

    assert claim_ledger.claim_citation_marker("C-2.4-001") == "[[CLAIM:C-2.4-001]]"
    assert claim_ledger.ClaimLedger().source_index_markdown() == (
        "## 证据与来源索引\n\n"
        "### 脚注对应关系\n\n"
        "本报告无关键脚注。\n\n"
        "### 项目证据 E-*\n\n"
        "本报告未引用此类来源。\n\n"
        "### 本地参考 R-*\n\n"
        "本报告未引用此类来源。\n\n"
        "### 网络来源 W-*\n\n"
        "本报告未引用此类来源。"
    )

    request = reporting.ReportRequest(
        operation="module_report",
        instruction="生成 2.4",
        target_modules=["2.4"],
        missing_evidence_policy="block",
    )
    coverage_result = coverage.evaluate_coverage(request, [])
    assert {
        "module": coverage_result.entries["2.4"].status.value,
        "target": coverage_result.entries["2.4"].submodules["2.4.1.1"].status.value,
        "unrequested": coverage_result.entries["2.1"].status.value,
    } == {"module": "blocked", "target": "blocked", "unrequested": "pending"}

    before = agentic.ModuleSubmission.model_construct(
        module_id="2.4",
        submodule_narratives={
            submodule_id: "unchanged"
            for submodule_id in taxonomy.REPORT_TAXONOMY["2.4"].submodules
        },
        claims=[],
        source_ids=["R-old"],
        revision=0,
    )
    revised = before.model_copy(deep=True)
    revised.revision = 1
    revised.submodule_narratives["2.4.1.1"] = "changed"
    revised.source_ids = ["R-new"]
    assert revision_diff.build_revision_diff(before, revised) == {
        "module_id": "2.4",
        "from_revision": 0,
        "to_revision": 1,
        "changed_submodule_narratives": ["2.4.1.1"],
        "changed_claim_ids": [],
        "source_ids_added": ["R-new"],
        "source_ids_removed": ["R-old"],
    }

    assert cross.cross_lane_specialization("2.1").prompt_context() == (
        '<cross_lane_specialization owner_module_id="2.1">\n'
        "责任落点：2.1 配电系统架构问题\n"
        "这不是模块写作 Skill，也不授权重审该模块的局部专业质量。读取五个已批准模块，"
        "只发现跨模块关系对本责任模块的判断、风险、行动、前提或验收造成的接口缺陷。\n"
        "- 核对负荷分配、关键供电路径和备用切换结论是否吸收 2.2 工况、2.3 保护、"
        "2.4 设备能力及 2.5 操作条件。\n"
        "- 核对双电源、自动切换、防并联和恢复路径是否与保护配合、机械/电气闭锁及 "
        "SOP/EOP 的实际接口一致。\n"
        "- 核对无功补偿、冲击负荷等架构措施是否遗漏谐波、保护动作、元件耐受和联合验证依赖。\n"
        "finding 的 owner_module_id 必须等于本责任模块，target_submodule_ids 也只能属于"
        "本责任模块。已完整写回且无需返工的关系，只有在本责任模块是 involved modules "
        "中编号最小者时才提交 synthesis_input，以避免多个 lane 重复申报同一关系。\n"
        "</cross_lane_specialization>"
    )
    assert final.final_lane_specialization("1").prompt_context() == (
        '<final_lane_specialization chapter_id="1">\n'
        "审查范围：Chapter 1 配电评估概述\n"
        "继续使用完整 final-auditor Skill 的通用验收方法；以下问题只特化本章的"
        "审查重点，不创建新的写作方法，也不扩大 required_section_ids：\n"
        "- 核验 1.1 是否准确说明评估范围、证据边界、限制和阅读前提，且不把未验证信息写成既定事实。\n"
        "- 核验 1.2 是否忠实概括五个批准模块的主要发现、风险轻重和判断依据，"
        "避免遗漏、夸大或平均化专业差异。\n"
        "- 核验 1.3 是否只按有证据的区域或责任边界组织重点、优先行动和验证状态，"
        "并与第三章行动结论保持一致。\n"
        "接口上下文只用于发现本章应承担的失真、遗漏或矛盾，finding target 仍只能"
        "属于本章。\n"
        "</final_lane_specialization>"
    )
