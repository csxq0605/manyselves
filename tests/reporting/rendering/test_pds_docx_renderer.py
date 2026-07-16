import io
from pathlib import Path

import pytest
from docx import Document

from autoreport.core.reporting.agentic_models import ClaimRecord, SourceKind, SourceRecord
from autoreport.core.reporting.claim_ledger import ClaimLedger
from autoreport.core.reporting.rendering.packaged_docx import PackagedDocxCore
from autoreport.core.reporting.rendering.pds_docx_renderer import (
    ApprovedReport,
    PdsDocxRenderer,
    ReportPhoto,
    ReportTable,
)


def _approved_report(photo_path: Path) -> ApprovedReport:
    narratives = {
        module_id: f"模块 {module_id} 的专家自然分析保留原样。"
        for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
    }
    claims = [
        ClaimRecord(
            id=f"C-00{index}",
            module_id=module_id,
            text=narratives[module_id],
            claim_type="risk_judgment",
            source_ids=["E-001"],
        )
        for index, module_id in enumerate(("2.1", "2.2", "2.3", "2.4", "2.5"), 1)
    ]
    source = SourceRecord(
        id="E-001",
        kind=SourceKind.PROJECT_EVIDENCE,
        title="现场照片与检测表",
        locator="Inputs/设备.xlsx；工作表=问题；单元格=A2:F2；图片=IMG-1",
    )
    return ApprovedReport(
        title="配电安全专家咨询报告",
        overview="总编形成的概述保持不变。",
        module_narratives=narratives,
        conclusion="总编形成的结论保持不变。",
        ledger=ClaimLedger(claims=claims, sources=[source]),
        tables=[
            ReportTable(
                title="整改行动表",
                headers=["对象", "动作"],
                rows=[["1A2", "复核连接"]],
                source_ids=["E-001"],
                claim_ids=["C-004"],
            )
        ],
        photos=[
            ReportPhoto(
                id="IMG-1",
                path=photo_path,
                caption="1A2 柜现场状态",
                source_id="E-001",
                claim_ids=["C-004"],
            )
        ],
    )


def test_renderer_uses_handoff_core_preserves_prose_and_adds_superscript_index_table_photo(
    tmp_path: Path,
) -> None:
    template = tmp_path / "template.docx"
    Document().save(template)
    photo = tmp_path / "photo.png"
    from PIL import Image

    Image.new("RGB", (30, 20), color="red").save(photo)
    renderer = PdsDocxRenderer(
        PackagedDocxCore(template),
    )
    output = tmp_path / "final.docx"

    result = renderer.render(_approved_report(photo), output)

    assert result.output_path == output
    assert result.handoff_core_used is True
    assert result.protected_prose_verified is True
    rendered = Document(output)
    all_text = "\n".join(paragraph.text for paragraph in rendered.paragraphs)
    assert "模块 2.4 的专家自然分析保留原样。" in all_text
    assert "配电安全专家咨询报告" in all_text
    assert "总编形成的结论保持不变。" in all_text
    assert "项目证据 E-*" in all_text
    assert "Inputs/设备.xlsx；工作表=问题；单元格=A2:F2；图片=IMG-1" in all_text
    assert any(run.font.superscript and run.text == "1" for p in rendered.paragraphs for run in p.runs)
    assert any(table.cell(0, 0).text == "对象" for table in rendered.tables)
    assert len(rendered.inline_shapes) == 1

    second_output = tmp_path / "final-second.docx"
    second = renderer.render(_approved_report(photo), second_output)
    assert second.output_sha256 == result.output_sha256
    assert second_output.read_bytes() == output.read_bytes()


def test_renderer_rejects_untraceable_table() -> None:
    with pytest.raises(ValueError, match="table.*source"):
        ReportTable(title="无来源表", headers=["值"], rows=[["1"]])


def test_approved_report_requires_hidden_claim_ledger_across_all_five_modules(
    tmp_path: Path,
) -> None:
    report = _approved_report(tmp_path / "photo.png")
    report.ledger.claims.pop()

    with pytest.raises(ValueError, match="claim ledger.*2.1-2.5"):
        ApprovedReport.model_validate(report.model_dump())


def test_title_insertion_falls_back_when_handoff_template_has_no_title_style() -> None:
    document = Document()
    document.styles["Title"].delete()

    PdsDocxRenderer._ensure_title(document, "配电安全专家咨询报告")

    assert document.paragraphs[0].text == "配电安全专家咨询报告"


def test_failed_post_render_validation_does_not_publish_output(tmp_path: Path) -> None:
    class CorruptCore:
        def render_approved_prose(self, *args, **kwargs):
            buffer = io.BytesIO()
            Document().save(buffer)
            return "broken.docx", buffer.getvalue()

    photo = tmp_path / "photo.png"
    from PIL import Image

    Image.new("RGB", (20, 20), color="blue").save(photo)
    output = tmp_path / "must-not-exist.docx"

    with pytest.raises(ValueError, match="changed or omitted protected"):
        PdsDocxRenderer(CorruptCore()).render(_approved_report(photo), output)
    assert not output.exists()


def test_packaged_core_explicitly_forbids_structured_model_prose_generation(
    tmp_path: Path,
) -> None:
    template = tmp_path / "template.docx"
    Document().save(template)
    core = PackagedDocxCore(template)

    with pytest.raises(ValueError, match="structured-model prose generation is disabled"):
        core.render_approved_prose("approved", report_model={"issues": []})


def test_packaged_core_renders_markdown_without_external_source(tmp_path: Path) -> None:
    template = tmp_path / "template.docx"
    Document().save(template)

    name, data = PackagedDocxCore(template).render_approved_prose(
        "# 标题\n\n正文\n\n| 对象 | 动作 |\n| --- | --- |\n| 1A2 | 复核连接 |"
    )

    rendered = Document(io.BytesIO(data))
    assert name == "配电安全专家咨询报告.docx"
    assert "正文" in "\n".join(paragraph.text for paragraph in rendered.paragraphs)
    assert rendered.tables[0].cell(1, 1).text == "复核连接"
