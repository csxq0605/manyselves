"""The judgment-ledger report runs end to end on the generic Runtime.

Every Agent turn is scripted; Tools, conversations, forks, map, parallel,
targeted revisions and delivery are the production ones.
"""

from __future__ import annotations

import asyncio
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from manyselves.capabilities.distribution_reporting import CAPABILITY_FILE
from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.ledger_report import (
    ledger_report_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    EvidenceItem,
    ReportRequest,
)
from manyselves.config.schema import AgentDefaults
from manyselves.kernel.definitions import load_capability
from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView
from tests.capabilities.distribution_reporting.test_public_reporting_runtime import (
    _write_complete_template_skill_fixture,
)
from tests.runtime.scripted_loop import ScriptedLoop

RUN_ID = "report-ledger-run"
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "emf": "image/x-emf",
    "wmf": "image/x-wmf",
}


def assert_media_parts_are_named_by_type(docx: Path) -> None:
    """Every image part ends with the extension its content type declares."""

    from xml.etree import ElementTree

    with zipfile.ZipFile(docx) as archive:
        types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
        ns = "{http://schemas.openxmlformats.org/package/2006/content-types}"
        defaults = {
            node.get("Extension").lower(): node.get("ContentType")
            for node in types.iter(f"{ns}Default")
        }
        overrides = {
            node.get("PartName"): node.get("ContentType")
            for node in types.iter(f"{ns}Override")
        }
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
    for name in media:
        stem, dot, extension = name.rpartition(".")
        assert dot and stem and extension, name
        assert extension.lower() in MEDIA_TYPES, name
        declared = overrides.get(f"/{name}") or defaults.get(extension.lower())
        assert declared == MEDIA_TYPES[extension.lower()], (name, declared)


def _semantic(text: str) -> str:
    text = re.sub(r"\[\^\d+\]", "", text).replace("**", "")
    text = re.sub(r"^#{1,6}\s*|^[-*]\s+|^\d+(?:\.\d+)*\.?\s+", "", text.strip())
    return re.sub(r"\s+", "", text)


def markdown_text_lines(markdown: str) -> list[str]:
    """Visible content of the delivered Markdown, one entry per block or cell."""

    values = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped or re.match(r"^\[\^\d+\]:", stripped) or stripped.startswith("#"):
            continue
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            values.extend(cell for cell in cells if cell)
            continue
        values.append(stripped)
    return [value for value in (_semantic(item) for item in values) if value]


def docx_body_text_lines(docx: Path) -> list[str]:
    """Visible body content of the DOCX after the table of contents."""

    from docx import Document
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(docx)
    children = list(document.element.body.iterchildren())

    def style_of(child: Any) -> str:
        paragraph = Paragraph(child, document)
        return paragraph.style.name if paragraph.style is not None else ""

    toc = [
        index
        for index, child in enumerate(children)
        if isinstance(child, CT_P) and style_of(child).lower().startswith("toc")
    ]
    values: list[str] = []
    for child in children[toc[-1] + 1 :]:
        if isinstance(child, CT_P):
            if not style_of(child).startswith("Heading"):
                values.append(Paragraph(child, document).text)
        elif isinstance(child, CT_Tbl):
            for row in Table(child, document).rows:
                values.extend(cell.text for cell in row.cells if cell.text.strip())
    return [value for value in (_semantic(item) for item in values) if value]


def _evidence() -> list[EvidenceItem]:
    items = []
    for number, module_id in enumerate(REPORT_MODULE_IDS, start=1):
        submodule_id = next(iter(REPORT_TAXONOMY[module_id].submodules))
        items.append(
            EvidenceItem(
                id=f"E-{number:04d}",
                subject=f"设备{number}",
                fact=f"设备{number}现场检查记录完整",
                source={"file_id": "table", "path": "Inputs/table.xlsx", "row": number},
                module_id=module_id,
                submodule_id=submodule_id,
            )
        )
    return items


def _input_of(content: str) -> dict[str, Any]:
    match = re.search(r"<input contract=\"[^\"]+\">\n(.*?)\n</input>", content, re.S)
    assert match is not None
    return json.loads(match.group(1))


def _task(content: str) -> str:
    match = re.search(r'<task id="([^"]+)">', content)
    assert match is not None
    return match.group(1)


def _row(module_id: str, number: int) -> dict[str, Any]:
    submodule_id = next(iter(REPORT_TAXONOMY[module_id].submodules))
    row = {
        "id": f"J-{module_id}-1",
        "submodule_id": submodule_id,
        "object": f"设备{number}",
        "checkpoint": "外观",
        "disposition": "no_concern",
        "evidence_status": "现场确认",
        "evidence_ids": [f"E-{number:04d}"],
        "basis": "记录完整。",
    }
    if module_id == "2.5":
        # A data gap: with the default "draft" policy the report is a draft.
        row.update(
            disposition="undetermined",
            evidence_status="资料未提供",
            gap={
                "missing": "缺少SOP文件",
                "impact": "无法判断操作规程是否完善",
                "requested_input": "提供现行SOP/EOP文件",
            },
        )
    if module_id == "2.4":
        row.update(
            disposition="concern",
            risk_level="中",
            hazard="可能过热。",
            action={"measure": "复测温度", "priority": "重要，紧急", "timeframe": "一周内"},
        )
    return row


def _answer(message):
    content = message.content
    task = _task(content)
    value = _input_of(content)
    if task in {"read-project", "read-ledger"}:
        return ("submit", [{"summary": "已读。"}])
    if task == "judge-module":
        module_id = value["module_id"]
        number = REPORT_MODULE_IDS.index(module_id) + 1
        return ("submit", [{"module_id": module_id, "rows": [_row(module_id, number)]}])
    if task == "review-judgments":
        findings = {}
        if value["module_id"] == "2.4":
            findings["J-2.4-1"] = {
                "problem": "依据没有说明温度。",
                "severity": "blocking",
                "suggestion": "写明依据。",
            }
        return ("submit", [{"findings": findings}])
    if task == "revise-judgments":
        assert set(value["findings"]) == {"J-2.4-1"}
        row = _row("2.4", REPORT_MODULE_IDS.index("2.4") + 1)
        row["basis"] = "记录显示温度偏高，需要复测。"
        return ("submit", [{"rows": [row]}])
    if task == "write-section":
        objects = "、".join(row["object"] for row in value["rows"]) or "本节"
        return (
            "submit",
            [{"submodule_id": value["submodule_id"], "text": f"{objects}检查情况如下。"}],
        )
    if task in {"chief-chapter-3", "revise-chief-actions"}:
        return (
            "submit",
            [
                {
                    "risk_overview": "总体风险可控。",
                    "action_lead": "建议措施如下。",
                    "actions": [
                        {
                            "priority": "重要，紧急",
                            "content": "复测",
                            "problem": "温度偏高",
                            "hazard": "可能过热",
                            "solution": "复测温度",
                            "row_ids": ["J-2.4-1"],
                        }
                    ],
                }
            ],
        )
    if task in {"chief-chapter-1", "revise-chief-overview"}:
        return (
            "submit",
            [
                {
                    "assessment_background": "本次评估覆盖五个维度。",
                    "overall_assessment": "总体良好。",
                    "dimension_health": [
                        {
                            "module_id": module_id,
                            "status": "良好",
                            "key_indicators": "记录完整",
                            "risk_level": "中" if module_id == "2.4" else "低",
                        }
                        for module_id in REPORT_MODULE_IDS
                    ],
                    "key_risks": [
                        {
                            "description": "温度偏高",
                            "hazard": "可能过热",
                            "action": "复测",
                            "row_ids": ["J-2.4-1"],
                        }
                    ],
                    "regional_executive_summary": "各区域情况正常。",
                }
            ],
        )
    if task == "edit-sections":
        findings = {}
        if value["owner"] == "2.1":
            section_id = next(iter(value["sections"]))
            findings[section_id] = {
                "problem": "表述太简单。",
                "severity": "blocking",
                "suggestion": "补一句结论。",
            }
        return ("submit", [{"findings": findings}])
    if task == "revise-sections":
        (section_id,) = value["findings"]
        return ("submit", [{"sections": {section_id: "设备1检查情况良好，无需处理。"}}])
    raise AssertionError(f"unexpected task {task}")


async def run_ledger_report(tmp_path: Path):
    """Run the ledger report with scripted agents; return binding, run id and loops."""

    _write_complete_template_skill_fixture(tmp_path)
    evidence = _evidence()
    evidence_path = tmp_path / f"Work/runs/{RUN_ID}/preparation/evidence.jsonl"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text(
        "".join(item.model_dump_json() + "\n" for item in evidence), encoding="utf-8"
    )
    state = {
        "run_id": RUN_ID,
        "request": ReportRequest(instruction="写一份完整报告。").model_dump(mode="json"),
        "evidence_items": [item.model_dump(mode="json") for item in evidence],
        "photo_assets": [],
    }

    bus = MessageBus()
    loops: list[ScriptedLoop] = []

    def build(**kwargs: Any) -> ScriptedLoop:
        loop = ScriptedLoop(bus, _answer, kwargs)
        loops.append(loop)
        return loop

    binding = GenericCapabilityRuntimeBinding(
        capability_file=CAPABILITY_FILE,
        workspace=tmp_path,
        services=RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        tool_implementations=ledger_report_tool_implementations(
            tmp_path, input_snapshot=None, snapshot_content=None
        ),
        loop_builder=build,
        start_input=lambda run_id, workflow_id, values: {"state": values},
    )
    # The ledger subworkflow is reached from full-report; run it directly here.
    binding.entrypoints = (*binding.entrypoints, "report-ledger")
    bus_task = asyncio.create_task(bus.process_queue())
    try:
        accepted = await binding.start(
            UUID("60000000-0000-4000-8000-000000000001"), "report-ledger", state
        )
    finally:
        await binding.close()
        bus.shutdown()
        await bus_task
    return binding, accepted, loops


@pytest.mark.asyncio
async def test_ledger_report_judges_reviews_writes_once_and_delivers(tmp_path: Path) -> None:
    binding, accepted, loops = await run_ledger_report(tmp_path)
    run = binding.get_run(accepted["run_id"])
    assert run["run"]["status"] == "completed", run["state"].get("error")
    outputs = binding.get_outputs(accepted["run_id"])["outputs"]
    result = next(item["value"] for item in outputs if item["id"] == "result")
    assert result["delivery_status"]
    artifacts = [item for item in outputs if item["kind"] == "artifact"]
    assert artifacts and all(item["exists"] for item in artifacts)

    # Rendering verification is reported with the delivered result.
    assert result["render_verification"]["protected_prose_verified"] is True
    assert result["render_verification"]["validation_warnings"] == []
    completion = json.loads(
        (tmp_path / result["delivery_completion_ref"]).read_text(encoding="utf-8")
    )
    assert completion["render_verification"] == result["render_verification"]

    markdown = (tmp_path / "Outputs/Reports/配电安全专家咨询报告.md").read_text(
        encoding="utf-8"
    )
    docx = tmp_path / "Outputs/Reports/配电安全专家咨询报告.docx"
    # The draft is marked the same way in both files.
    assert markdown.startswith("# 配电安全专家咨询报告（草稿）\n\n草稿：本报告存在资料缺口")
    with zipfile.ZipFile(docx) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
        footnotes_xml = archive.read("word/footnotes.xml").decode("utf-8")
        headers = "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if re.fullmatch(r"word/header\d*\.xml", name)
        )
        rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        content_types = archive.read("[Content_Types].xml").decode("utf-8")
        visible = "".join(
            text
            for name in archive.namelist()
            if re.fullmatch(r"word/[a-z]+\d*\.xml", name)
            for text in re.findall(
                r"<w:t(?: [^>]*)?>([^<]*)</w:t>", archive.read(name).decode("utf-8")
            )
        )
    assert "配电安全专家咨询报告（草稿）" in document_xml
    assert "草稿：本报告存在资料缺口" in headers
    # The cover carries no template placeholder; unknown facts say 待补.
    assert not re.search(r"X{3,}|〈[^〉]*〉", visible)
    assert "待补" in document_xml
    # Citations are Word footnotes with the source of each cited claim.
    assert 'relationships/footnotes" Target="footnotes.xml"' in rels
    assert "/word/footnotes.xml" in content_types
    assert document_xml.count("<w:footnoteReference ") == 5
    assert "E-0001 设备1（Inputs/table.xlsx）" in footnotes_xml
    assert "设备1检查情况良好，无需处理。[^1]" in markdown
    assert "[^1]: E-0001 设备1（Inputs/table.xlsx）" in markdown
    assert "[[CITE" not in markdown and "[^" not in visible
    # The DOCX shows exactly the delivered Markdown's text.
    assert docx_body_text_lines(docx) == markdown_text_lines(markdown)
    assert_media_parts_are_named_by_type(docx)

    ledger_dir = tmp_path / f"Work/runs/{RUN_ID}/ledger"
    final = json.loads((ledger_dir / "judgments.json").read_text(encoding="utf-8"))
    assert final["2.4"][0]["basis"] == "记录显示温度偏高，需要复测。"

    def tasks_of(loop: ScriptedLoop) -> list[str]:
        return [_task(message.content) for message in loop.received]

    shared = loops[0]
    assert tasks_of(shared) == ["read-project"]
    authors = {
        _input_of(loop.received[0].content)["module_id"]: loop
        for loop in loops
        if tasks_of(loop)[:1] == ["judge-module"]
    }
    assert set(authors) == set(REPORT_MODULE_IDS)
    for loop in authors.values():
        # Every author starts from the shared project reading.
        assert loop.restored[0][0].content == shared.received[0].content
    # 2.4 revised only in its own conversation; 2.1 revised its text there too.
    assert "revise-judgments" in tasks_of(authors["2.4"])
    assert "revise-judgments" not in tasks_of(authors["2.1"])
    assert "revise-sections" in tasks_of(authors["2.1"])
    assert "revise-sections" not in tasks_of(authors["2.4"])
    # Prose is written once per section, after judgment, in the author's conversation.
    sections = len(REPORT_TAXONOMY["2.4"].submodules)
    assert tasks_of(authors["2.4"]).count("write-section") == sections
    chief = [loop for loop in loops if "chief-chapter-3" in tasks_of(loop)]
    assert len(chief) == 1
    assert tasks_of(chief[0])[:2] == ["chief-chapter-3", "chief-chapter-1"]


def test_full_report_reaches_only_ledger_agents_and_bound_tools() -> None:
    from manyselves.kernel.definitions import DefinitionKind
    from manyselves.kernel.executors import build_builtin_executor_registry
    from manyselves.kernel.workflow import WorkflowCompiler

    _capability, registry = load_capability(CAPABILITY_FILE)
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        registry.require(DefinitionKind.WORKFLOW, "full-report"), registry
    )
    plans = [plan, *plan.subworkflow_plans.values()]
    agents = {agent for candidate in plans for agent in candidate.agent_ids}
    tools = {tool for candidate in plans for tool in candidate.tool_ids}
    assert agents == {"photo-reader", "report-consultant"}
    implementations = ledger_report_tool_implementations(
        Path("."), input_snapshot=None, snapshot_content=None
    )
    for tool_id in tools:
        definition = registry.require(DefinitionKind.TOOL, tool_id)
        assert definition.implementation.rsplit(":", 1)[-1] in implementations, tool_id


def test_program_and_reviewer_findings_on_one_target_are_kept_together(tmp_path: Path) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import LedgerTools

    merged = LedgerTools(tmp_path).merge_findings(
        {
            "item": {
                "program_findings": {
                    "2.5.1": {"problem": "数量不符。", "severity": "minor", "suggestion": "改数量。"},
                    "2.5.2": {"problem": "缺依据。", "severity": "minor", "suggestion": "补依据。"},
                }
            },
            "review": {
                "findings": {
                    "2.5.1": {"problem": "结论过强。", "severity": "blocking", "suggestion": "改措辞。"}
                }
            },
        }
    )

    assert list(merged["findings"]) == ["2.5.1", "2.5.2"]
    both = merged["findings"]["2.5.1"]
    assert both["severity"] == "blocking"
    assert "数量不符" in both["problem"] and "结论过强" in both["problem"]
    assert both["suggestion"] == "改数量。；改措辞。"
    assert merged["blocking"]
    assert set(merged["revision_input"]["findings"]) == {"2.5.1"}
    assert set(merged["revision_input"]["minor"]) == {"2.5.2"}


def test_review_gets_the_original_images_only_for_doubtful_readings() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import JudgmentRow
    from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import (
        photos_to_check,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.photo_reading import (
        PhotoReading,
    )

    rows = [
        JudgmentRow.model_validate(
            {"id": "J-2.4-1", "submodule_id": "2.4.1", "object": "14#柜", "checkpoint": "剩余电流",
             "disposition": "no_concern", "evidence_status": "现场确认", "evidence_ids": ["E-1"], "basis": "记录 11.3A。"}
        )
    ]
    reference = {
        "evidence_photos": {"E-1": ["P-1", "P-2"]},
        "photos": {
            "P-1": {"path": "Work/a/P-1.png", "media_type": "image/png"},
            "P-2": {"path": "Work/a/P-2.png", "media_type": "image/png"},
        },
    }

    def reading(photo_id: str, match: str) -> PhotoReading:
        return PhotoReading(photo_id=photo_id, visible="表计", record_match=match,
                            reason="r", needs_review=match == "conflicts")

    photos = photos_to_check(
        rows, reference, {"P-1": reading("P-1", "conflicts"), "P-2": reading("P-2", "matches")}
    )
    assert [(p["photo_id"], p["row_ids"], p["path"]) for p in photos] == [
        ("P-1", ["J-2.4-1"], "Work/a/P-1.png")
    ]
