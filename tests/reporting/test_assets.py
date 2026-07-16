from pathlib import Path

import pytest

from autoreport.core.reporting.agentic_models import (
    ClaimRecord,
    EditedReportSubmission,
    TableSubmission,
)
from autoreport.core.reporting.assets import (
    ReportAssetAssembler,
    validate_editor_protection,
)
from autoreport.core.reporting.models import EvidenceItem, PhotoAsset, SourceLocation


def _edited(**updates) -> EditedReportSubmission:
    values = {
        "title": "配电安全专家咨询报告",
        "overview": "概述",
        "module_narratives": {
            module_id: f"模块 {module_id} 正文" for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
        },
        "conclusion": "结论",
    }
    values.update(updates)
    return EditedReportSubmission(**values)


def _claim() -> ClaimRecord:
    return ClaimRecord(
        id="C-2.4-001",
        module_id="2.4",
        submodule_id="2.4.2.3",
        text="1A2 柜连接状态需要复核",
        claim_type="risk_judgment",
        source_ids=["E-0001"],
    )


def test_editor_must_protect_every_approved_claim() -> None:
    with pytest.raises(ValueError, match="protected claim"):
        validate_editor_protection(_edited(), [_claim()])


def test_editor_anchor_must_occur_once_in_claim_module() -> None:
    edited = _edited(
        protected_claim_ids=["C-2.4-001"],
        citation_anchors={"C-2.4-001": "重复锚点"},
        module_narratives={
            "2.1": "模块 2.1 正文",
            "2.2": "模块 2.2 正文",
            "2.3": "模块 2.3 正文",
            "2.4": "重复锚点；重复锚点",
            "2.5": "模块 2.5 正文",
        },
    )

    with pytest.raises(ValueError, match="exactly once"):
        validate_editor_protection(edited, [_claim()])


def test_asset_assembler_builds_traceable_table_and_photo(tmp_path: Path) -> None:
    photo_path = tmp_path / "Work/assets/IMG-1.png"
    photo_path.parent.mkdir(parents=True)
    photo_path.write_bytes(b"image")
    claim = _claim()
    evidence = EvidenceItem(
        id="E-0001",
        subject="1A2 柜",
        fact="连接点存在异常",
        source=SourceLocation(file_id="F-1", path=Path("Inputs/check.xlsx"), cell="A2"),
        module_id="2.4",
        submodule_id="2.4.2.3",
        photo_refs=["IMG-1"],
    )
    asset = PhotoAsset(
        id="IMG-1",
        path=photo_path.relative_to(tmp_path),
        sha256="abc",
        media_type="image/png",
        source_member="media/image1.png",
    )
    edited = _edited(
        protected_claim_ids=[claim.id],
        citation_anchors={claim.id: "连接状态需要复核"},
        module_narratives={
            "2.1": "模块 2.1 正文",
            "2.2": "模块 2.2 正文",
            "2.3": "模块 2.3 正文",
            "2.4": "1A2 柜连接状态需要复核",
            "2.5": "模块 2.5 正文",
        },
        photo_ids=["IMG-1"],
        tables=[
            TableSubmission(
                title="整改行动表",
                headers=["对象", "动作"],
                rows=[["1A2", "复核连接"]],
                source_ids=["E-0001"],
                claim_ids=[claim.id],
            )
        ],
    )

    tables, photos = ReportAssetAssembler(tmp_path).build([evidence], [asset], [claim], edited)

    assert tables[0].source_ids == ["E-0001"]
    assert photos[0].path == photo_path
    assert photos[0].source_id == "E-0001"
    assert photos[0].claim_ids == [claim.id]
