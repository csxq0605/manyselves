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
from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
    bound_report_taxonomy,
    example_report_taxonomy,
)
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
from tests.capabilities.distribution_reporting.support import (
    write_role_skill_fixture as _write_complete_template_skill_fixture,
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
            finding="温度偏高",
            hazard="可能过热，待核实。",
            hazard_conditions=["温度持续偏高"],
            action={"measure": "复测温度", "priority": "重要，紧急", "timeframe": "一周内"},
        )
    return row


def _answer(message):
    content = message.content
    task = _task(content)
    value = _input_of(content)
    if task == "review-cross-module":
        # The rows are in the shared prefix; the slice says what to decide.
        if "J-2.4-1" in value["focus_rows"] and "J-2.5-1" in content:
            return ("submit", [{"findings": {}, "proposals": [{
                "row_id": "J-2.4-1", "other_row_id": "J-2.5-1", "relation": "缺资料影响判断",
                "reason": "缺少SOP使温度偏高的处置无法核对。",
                "mechanism": "处置依据缺失", "condition": "两边都缺同一类资料",
            }]}])
        return ("submit", [{"findings": {}, "proposals": []}])
    if task == "review-objects":
        return ("submit", [{"findings": {}}])
    if task == "judge-module":
        module_id = value["module_id"]
        number = REPORT_MODULE_IDS.index(module_id) + 1
        return ("submit", [{"module_id": module_id, "rows": [_row(module_id, number)]}])
    if task == "relate-judgments":
        return ("submit", [{"relations": {}}])
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
        offers = value.get("proposed") or {}
        if value.get("fact_check"):
            assert set(value["findings"]) == {"J-2.4-1"}
            return ("submit", [{"patches": [
                {"row_id": "J-2.4-1", "fields": {"finding": "温度偏高，待复测确认。"}}
            ]}])
        if not value["findings"]:
            # Only a proposal sends 2.5 back; it declines.
            assert set(offers) == {"J-2.5-1"}
            return ("submit", [{"declined_proposals": [{
                "row_id": "J-2.5-1", "other_row_id": "J-2.4-1", "reason": "SOP 缺失与温度无关。",
            }]}])
        # Only blocking findings send an author back, with the proposals on its rows.
        assert set(value["findings"]) == {"J-2.4-1"}
        (offer,) = offers["J-2.4-1"]
        return ("submit", [{
            "patches": [
                {"row_id": "J-2.4-1", "fields": {"basis": "记录显示温度偏高，需要复测。"}}
            ],
            "relations": {"J-2.4-1": [{
                "row_id": offer["row_id"], "relation": offer["relation"],
                "reason": "缺少SOP使温度偏高的处置无法核对。",
                "mechanism": "处置依据缺失", "condition": "两边都缺同一类资料",
            }]},
        }])
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
                    "risk_summary": ["总体风险可控。"],
                    "risk_details": [{"module_id": "2.4", "points": ["温度偏高，需复测。"]}],
                    "action_lead": "建议措施如下。",
                    "actions": [
                        {
                            "group_id": group["group_id"],
                            "problem": "温度偏高",
                            "hazard": "若温度持续偏高，可能过热",
                            "solution": "复测温度",
                        }
                        for group in (value.get("action_groups") or [])
                    ],
                }
            ],
        )
    if task == "revise-chief-facts":
        # One turn answers the fact check of both chapters, on the version checked.
        assert set(value["findings"]) == {"1.2-重点风险-1"}
        assert value["findings"]["1.2-重点风险-1"]["grounds"]["J-2.4-1"]
        # Only what the findings point at, by place id: the risk itself and
        # the 3.2 group that cites the same row (same_elsewhere); no section text.
        assert value["units"] == {}
        assert set(value["key_risks"]) == {"1.2-重点风险-1"}
        assert "chapter1" not in value and "texts" not in value
        assert [action["group_id"] for action in value["actions"]] == ["G-01"]
        risk = {**value["key_risks"]["1.2-重点风险-1"], "description": "个别对象温度偏高需复测"}
        return ("submit", [{"key_risks": {"1.2-重点风险-1": risk}}])
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
    if task == "check-chief-facts":
        findings = {}
        for unit in value["units"]:
            if unit["unit_id"] == "1.2-重点风险-1":
                findings[unit["unit_id"]] = {
                    "problem": "风险等级写重了。",
                    "severity": "blocking",
                    "suggestion": "按所引行写。",
                    "grounds": ["J-2.4-1"],
                    "rows_also": ["J-2.4-1"],
                }
        return ("submit", [{"findings": findings}])
    if task == "edit-sections":
        findings = {}
        if value["owner"] == "2.1":
            section_id = next(iter(value["sections"]))
            findings[section_id] = {
                "problem": "表述太简单。",
                "severity": "blocking",
                "suggestion": "补一句结论。",
                "grounds": ["J-2.1-1"],
            }
        return ("submit", [{"findings": findings}])
    if task == "revise-sections":
        (section_id,) = value["findings"]
        return ("submit", [{"sections": {section_id: "设备1检查情况良好，无需处理。"}}])
    raise AssertionError(f"unexpected task {task}")


async def run_ledger_report(
    tmp_path: Path, answer=None, evidence=None, extra_state=None, run_id: str = RUN_ID
):
    """Run the ledger report with scripted agents; return binding, run id and loops."""

    answer = answer or _answer
    _write_complete_template_skill_fixture(tmp_path)
    evidence = evidence or _evidence()
    evidence_path = tmp_path / f"Work/runs/{run_id}/preparation/evidence.jsonl"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        "".join(item.model_dump_json() + "\n" for item in evidence), encoding="utf-8"
    )
    state = {
        "run_id": run_id,
        "request": ReportRequest(instruction="写一份完整报告。").model_dump(mode="json"),
        "evidence_items": [item.model_dump(mode="json") for item in evidence],
        "photo_assets": [],
        **(extra_state or {}),
    }

    bus = MessageBus()
    loops: list[ScriptedLoop] = []

    def build(**kwargs: Any) -> ScriptedLoop:
        loop = ScriptedLoop(bus, answer, kwargs)
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
    # The chief's answer to the fact check reaches the report.
    assert "个别对象温度偏高需复测" in markdown
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
    fixed = json.loads((ledger_dir / "fact-row-fixes.json").read_text(encoding="utf-8"))
    assert [row["id"] for row in fixed["rows"]] == ["J-2.4-1"]
    assert fixed["rows"][0]["finding"] == "温度偏高，待复测确认。"
    assert list(fixed["sections"]) == ["2.4"]

    def tasks_of(loop: ScriptedLoop) -> list[str]:
        return [_task(message.content) for message in loop.received]

    authors = {
        _input_of(loop.received[0].content)["module_id"]: loop
        for loop in loops
        if tasks_of(loop)[:1] == ["judge-module"]
    }
    assert set(authors) == set(REPORT_MODULE_IDS)
    for module_id, loop in authors.items():
        # Every author starts a conversation of its own from its materials:
        # the project map, its records and the same objects' records elsewhere.
        assert not loop.restored
        first = loop.received[0].content
        assert '<context name="materials">' in first and "# 项目图谱" in first
        assert f"E-{REPORT_MODULE_IDS.index(module_id) + 1:04d}" in first
    # Each author related right after judging, with no proposals; the
    # cross-module review read one shared prefix and proposed what was
    # missing, which reached both ends in their one revision.
    crosses = [loop for loop in loops if tasks_of(loop) == ["review-cross-module"]]
    assert len(crosses) == 1 and not crosses[0].restored
    cross = crosses[0].received[0].content
    # Shared prefix first, then the task, then what this slice decides.
    assert cross.index('<context name="rows">') < cross.index("<task id=") < cross.index("<input contract=")
    relate_24 = next(
        _input_of(m.content) for m in authors["2.4"].received if _task(m.content) == "relate-judgments"
    )
    assert "proposed" not in relate_24
    assert tasks_of(authors["2.4"]).count("relate-judgments") == 1
    settle_24 = next(
        _input_of(m.content) for m in authors["2.4"].received if _task(m.content) == "revise-judgments"
    )
    (offer,) = settle_24["proposed"]["J-2.4-1"]
    assert offer["row_id"] == "J-2.5-1" and offer["other_row"]["id"] == "J-2.5-1"
    assert "J-2.5-1" in settle_24["proposal_facts"] or settle_24["proposal_facts"]
    outcome = json.loads((ledger_dir / "cross-proposals-outcome.json").read_text(encoding="utf-8"))
    assert outcome["proposals_kept"] == 1
    assert outcome["relations_added"] == 1 and outcome["declined"] == 1
    assert [r["row_id"] for r in final["2.4"][0]["relations"]] == ["J-2.5-1"]
    # 2.4 revised only in its own conversation; 2.1 had nothing blocking and
    # no proposal; 2.5 revised for the proposal alone.
    assert "revise-judgments" in tasks_of(authors["2.4"])
    assert "revise-judgments" in tasks_of(authors["2.5"])
    assert "revise-judgments" not in tasks_of(authors["2.1"])
    # Before review, every author weighs what other modules flagged
    # (the concern of 2.4, the data gap of 2.5) in its own conversation.
    relate_21 = next(
        _input_of(m.content) for m in authors["2.1"].received if _task(m.content) == "relate-judgments"
    )
    assert sorted(line.split()[0] for line in relate_21["flagged_elsewhere"]) == ["J-2.4-1", "J-2.5-1"]
    assert tasks_of(authors["2.1"]).index("relate-judgments") == 1
    # Each review starts in a fresh conversation with the rows and what they cite.
    reviews = [loop for loop in loops if tasks_of(loop) == ["review-judgments"]]
    assert len(reviews) == len(REPORT_MODULE_IDS)
    assert all(not loop.restored for loop in reviews)
    review_24 = next(
        loop.received[0].content for loop in reviews
        if _input_of(loop.received[0].content)["module_id"] == "2.4"
    )
    assert '<context name="facts">' in review_24 and "E-0004" in review_24
    assert _input_of(review_24)["rows"][0]["id"] == "J-2.4-1"
    def module_of(section_id: str) -> str:
        return next(m for m in REPORT_MODULE_IDS if section_id in REPORT_TAXONOMY[m].submodules)

    writers = {
        module_of(_input_of(loop.received[0].content)["submodule_id"]): loop
        for loop in loops
        if tasks_of(loop)[:1] == ["write-section"]
    }
    # Each writer revises once: 2.1 on the editor's finding.
    assert tasks_of(writers["2.1"]).count("revise-sections") == 1
    # 2.4 has no editor finding; it revises once, only for the fact-check row fix.
    fix_24 = [_input_of(m.content) for m in writers["2.4"].received if _task(m.content) == "revise-sections"]
    assert len(fix_24) == 1 and "温度偏高，待复测确认。" in json.dumps(fix_24[0], ensure_ascii=False)
    # Prose is written once per section, after judgment, by a writer that
    # starts from its group's material; the story line reaches its sections.
    sections = len(REPORT_TAXONOMY["2.4"].submodules)
    assert tasks_of(writers["2.4"]).count("write-section") == sections
    assert not writers["2.4"].restored
    assert '<context name="pack">' in writers["2.4"].received[0].content
    assert '<context name="pack">' not in writers["2.4"].received[1].content
    briefs = [_input_of(m.content) for m in writers["2.4"].received if _task(m.content) == "write-section"]
    assert [b["story_lines"][0]["other_sections"] for b in briefs if b.get("story_lines")] == [
        [next(iter(REPORT_TAXONOMY["2.5"].submodules))]
    ]
    story = json.loads((ledger_dir / "story-lines.json").read_text(encoding="utf-8"))
    assert [line["row_ids"] for line in story["lines"]] == [["J-2.4-1", "J-2.5-1"]]
    chief = [loop for loop in loops if "chief-chapter-3" in tasks_of(loop)]
    assert len(chief) == 1
    assert tasks_of(chief[0])[:2] == ["chief-chapter-3", "chief-chapter-1"]
    # The chief reads its own material once; chapter 1 does not resend it.
    assert not chief[0].restored
    chief_3, chief_1 = chief[0].received[:2]
    assert '<context name="map">' in chief_3.content
    assert [row["id"] for row in _input_of(chief_3.content)["rows"]] == ["J-2.4-1", "J-2.5-1"]
    assert set(_input_of(chief_1.content)) == {"note"}
    # Chapters 1 and 3 are fact-checked in small packets, each in a fresh
    # conversation; a blocking finding on chapter 1 sends the chief back once.
    checks = [loop for loop in loops if tasks_of(loop) == ["check-chief-facts"]]
    assert checks and all(not loop.restored for loop in checks)
    packet = _input_of(checks[0].received[0].content)
    assert packet["units"] and {"rows", "facts", "values", "counts"} <= set(packet)
    assert all("basis" in row for row in packet["rows"])
    # Chapter 3 is checked while chapter 1 is written, chapter 1 after it;
    # the two batches' packets never share an id and join for the one fix.
    packets = [_input_of(loop.received[0].content) for loop in checks]
    by_batch = {
        prefix: [p for p in packets if p["packet_id"].startswith(prefix)] for prefix in ("F3-", "F1-")
    }
    assert by_batch["F3-"] and by_batch["F1-"] and len(packets) == sum(map(len, by_batch.values()))
    assert all(u["unit_id"].startswith("3.") for p in by_batch["F3-"] for u in p["units"])
    assert all(u["unit_id"].startswith("1.") for p in by_batch["F1-"] for u in p["units"])
    joined = json.loads((ledger_dir / "chief-fact-packets.json").read_text(encoding="utf-8"))
    assert sorted(p["packet_id"] for p in joined) == sorted(p["packet_id"] for p in packets)
    # The chief has no edit pass of its own; one turn fixes both chapters.
    edits = [
        loop for loop in loops
        if tasks_of(loop) == ["edit-sections"]
        and _input_of(loop.received[0].content)["owner"] == "chief"
    ]
    assert edits == []
    # The fix runs in a conversation of its own, not in the chief's.
    assert "revise-chief-facts" not in tasks_of(chief[0])
    fixes = [loop for loop in loops if tasks_of(loop) == ["revise-chief-facts"]]
    assert len(fixes) == 1 and not fixes[0].restored
    # Each review reads its own module only.
    assert "related_rows" not in _input_of(review_24)
    assert '<context name="dossiers">' not in review_24


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


@pytest.mark.asyncio
async def test_other_modules_judgments_on_one_object_reach_the_author_and_the_writer(
    tmp_path: Path,
) -> None:
    """2.2 and 2.4 judge the same object from different checkpoints: they complement."""

    evidence = _evidence()
    shared = REPORT_MODULE_IDS.index("2.4")
    evidence[REPORT_MODULE_IDS.index("2.2")] = evidence[REPORT_MODULE_IDS.index("2.2")].model_copy(
        update={"subject": evidence[shared].subject}
    )
    seen: dict[str, Any] = {}

    def answer(message):
        task = _task(message.content)
        value = _input_of(message.content)
        if task == "review-judgments":
            # The module lens reads its own module only.
            seen.setdefault("module_lens", {})[value["module_id"]] = (
                '<context name="dossiers">' in message.content or "related_rows" in value
            )
        if task == "review-cross-module":
            seen.setdefault("review_related", []).extend(
                relation["row_id"] for relation in value["relations"]
            )
        if task == "relate-judgments" and "J-2.2-1" in value.get("related", {}):
            seen["related"] = value["related"]
            return ("submit", [{"relations": {"J-2.2-1": [
                {"row_id": "J-2.4-1", "relation": "负荷—发热", "reason": "2.4 记录的温度偏高与本行同期。"},
                {"row_id": "J-9.9-9", "relation": "负荷—发热", "reason": "不存在的行。"},
            ]}}])
        if task == "write-section" and value.get("related_rows"):
            seen.setdefault("related_rows", {})[value["submodule_id"]] = value["related_rows"]
        if task in {"chief-chapter-3", "revise-chief-actions"}:
            seen["chains"] = value.get("object_chains")
        return _answer(message)

    binding, accepted, _loops = await run_ledger_report(tmp_path, answer, evidence)
    run = binding.get_run(accepted["run_id"])
    assert run["run"]["status"] == "completed", run["state"].get("error")
    assert not any(seen["module_lens"].values())
    assert [entry["row_id"] for entry in seen["related"]["J-2.2-1"]] == ["J-2.4-1"]
    # The reference to a row that is not on this object is dropped.
    final = json.loads(
        (tmp_path / f"Work/runs/{RUN_ID}/ledger/judgments.json").read_text(encoding="utf-8")
    )
    assert [r["row_id"] for r in final["2.2"][0]["relations"]] == ["J-2.4-1"]
    # The cross-module review checks the authors' relations (2.2's own); the
    # relation it proposed is settled by the authors after it.
    assert sorted(seen["review_related"]) == ["J-2.4-1"]
    assert [
        [entry["row_id"] for entry in rows]
        for section_id, rows in seen["related_rows"].items() if section_id.startswith("2.2")
    ] == [["J-2.4-1"]]
    (chain,) = seen["chains"]
    assert {row["row_id"] for row in chain["rows"]} == {"J-2.2-1", "J-2.4-1"}


@pytest.mark.unbound_taxonomy
@pytest.mark.asyncio
async def test_every_step_reads_the_tree_of_the_run_and_writes_only_its_sections(
    tmp_path: Path,
) -> None:
    """Nothing is bound in the process: every tool binds the run's own tree.

    This run's table leaves out 2.4.3 and its subsections, so the report has
    no such heading and no placeholder for it.
    """

    tree = example_report_taxonomy()
    for module in tree["modules"]:
        module["sections"] = [
            s for s in module["sections"]
            if s["id"] != "2.4.3" and not s["id"].startswith("2.4.3.")
        ]

    def answer(message):
        # The scripted agents read the tree too; real agents read it from the prompt.
        with bound_report_taxonomy(tree):
            return _answer(message)

    with bound_report_taxonomy(tree):
        evidence = _evidence()
    # Preparation persists the run's tree beside its evidence.
    persisted = tmp_path / f"Work/runs/{RUN_ID}/preparation/report-taxonomy.json"
    persisted.parent.mkdir(parents=True)
    persisted.write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    binding, accepted, _loops = await run_ledger_report(
        tmp_path, answer=answer, evidence=evidence, extra_state={"report_taxonomy": tree}
    )
    run = binding.get_run(accepted["run_id"])
    assert run["run"]["status"] == "completed", run["state"].get("error")
    markdown = (tmp_path / "Outputs/Reports/配电安全专家咨询报告.md").read_text(encoding="utf-8")
    assert "2.4.2" in markdown
    assert not re.search(r"^#+ 2\.4\.3", markdown, re.M)


def test_chapter_1_is_checked_in_its_own_branch_without_waiting_for_chapter_3() -> None:
    from manyselves.kernel.definitions import DefinitionKind

    _capability, registry = load_capability(CAPABILITY_FILE)
    chief = registry.require(DefinitionKind.WORKFLOW, "ledger-chief")
    ids = [action["id"] for action in chief.actions]
    parallel = next(a for a in chief.actions if a["id"] == "chapter-1-and-facts-3")
    chapter_1 = ids[ids.index(parallel["branches"]["chapter1"]):ids.index("chapter-1-done")]
    chapter_3 = ids[ids.index(parallel["branches"]["facts3"]):ids.index("facts-3-done")]
    # Chapter 1 is written, then checked, in one branch; chapter 3's packets in the other.
    assert chapter_1[:3] == ["chapter-1", "check-1", "plan-facts"] and "fact-check" in chapter_1
    assert "fact-check-3" in chapter_3 and "chapter-1" not in chapter_3
    assert ids.index("apply-facts") > ids.index(parallel["join"])
