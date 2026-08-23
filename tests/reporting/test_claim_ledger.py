from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.agentic_models import (
    ClaimRecord,
    SourceKind,
    SourceRecord,
)
from manyselves.core.reporting.claim_ledger import CitationBindingError, ClaimLedger
from manyselves.core.reporting.models import EvidenceItem, PhotoAsset, SourceLocation


def _first_submodule(module_id: str) -> str:
    return next(iter(REPORT_TAXONOMY[module_id].submodules))


def _sources() -> list[SourceRecord]:
    return [
        SourceRecord(
            id="E-001",
            kind=SourceKind.PROJECT_EVIDENCE,
            title="低压柜检测记录",
            locator="Inputs/检测.xlsx；工作表=低配；单元格=H5:I5",
        ),
        SourceRecord(
            id="R-001",
            kind=SourceKind.LOCAL_REFERENCE,
            title="低压配电参考",
            locator="Knowledge/标准/低压.md；章节=接地；版本=2026-01",
        ),
        SourceRecord(
            id="W-001",
            kind=SourceKind.WEB,
            title="Manufacturer guide",
            locator="https://example.com/guide",
            publisher="Example Manufacturer",
            published_at="2026-01-02",
            accessed_at="2026-07-15",
        ),
    ]


def test_ledger_accepts_all_five_modules_and_builds_deterministic_citations() -> None:
    claims = [
        ClaimRecord(
            id=f"C-00{index}",
            module_id=module_id,
            submodule_id=_first_submodule(module_id),
            text=f"模块 {module_id} 的关键判断",
            claim_type="risk_judgment",
            source_ids=["E-001"],
        )
        for index, module_id in enumerate(("2.1", "2.2", "2.3", "2.4", "2.5"), 1)
    ]

    ledger = ClaimLedger(claims=claims, sources=_sources())
    plan = ledger.build_citation_plan()

    assert [entry.marker for entry in plan.entries] == [1, 2, 3, 4, 5]
    assert [entry.claim_id for entry in plan.entries] == [claim.id for claim in claims]
    assert "项目证据 E-*" in plan.evidence_index_markdown
    assert "Inputs/检测.xlsx；工作表=低配；单元格=H5:I5" in plan.evidence_index_markdown


def test_footnote_numbers_remain_contiguous_when_non_footnoted_claims_are_present() -> None:
    claims = [
        ClaimRecord(
            id="C-001",
            module_id="2.1",
            submodule_id="2.1.1",
            text="仅用于后台的不确定项",
            claim_type="technical_interpretation",
            source_ids=[],
            footnote_required=False,
            unresolved=True,
        ),
        ClaimRecord(
            id="C-002",
            module_id="2.1",
            submodule_id="2.1.1",
            text="需要脚注的现场判断",
            claim_type="risk_judgment",
            source_ids=["E-001"],
        ),
    ]
    plan = ClaimLedger(claims=claims, sources=_sources()).build_citation_plan()

    assert [(entry.marker, entry.claim_id) for entry in plan.entries] == [(1, "C-002")]


def test_ledger_rejects_unregistered_and_wrong_domain_sources() -> None:
    claim = ClaimRecord(
        id="C-001",
        module_id="2.1",
        submodule_id="2.1.1",
        text="现场主变负载率为 92%",
        claim_type="project_fact",
        source_ids=["E-404"],
    )

    with pytest.raises(ValueError, match="unknown source"):
        ClaimLedger(claims=[claim], sources=_sources())

    bad_web = SourceRecord(
        id="W-002",
        kind=SourceKind.WEB,
        title="网页",
        locator=str(Path("Inputs") / "not-a-url.xlsx"),
        accessed_at="2026-07-15",
    )
    with pytest.raises(ValueError, match="URL"):
        ClaimLedger(claims=[], sources=[bad_web])


def test_resolved_risk_judgment_requires_project_evidence() -> None:
    claim = ClaimRecord(
        id="C-001",
        module_id="2.5",
        submodule_id="2.5.1",
        text="运维闭环缺失形成高风险",
        claim_type="risk_judgment",
        source_ids=["R-001"],
    )

    with pytest.raises(ValueError, match="risk.*E-"):
        ClaimLedger(claims=[claim], sources=_sources())


def test_local_reference_accepts_any_knowledge_locator_and_rejects_other_roots() -> None:
    accepted = SourceRecord(
        id="R-009",
        kind=SourceKind.LOCAL_REFERENCE,
        title="设备手册",
        locator="Knowledge/供应商/低压柜.md；章节=维护",
    )
    ClaimLedger(claims=[], sources=[accepted])

    rejected = accepted.model_copy(update={"locator": "Inputs/低压柜.md"})
    with pytest.raises(ValueError, match="Knowledge"):
        ClaimLedger(claims=[], sources=[rejected])


def test_citation_binding_replaces_stable_claim_marker() -> None:
    claim = ClaimRecord(
        id="C-001",
        module_id="2.3",
        submodule_id="2.3.1",
        text="原模块判断",
        claim_type="risk_judgment",
        source_ids=["E-001", "R-001"],
    )
    ledger = ClaimLedger(claims=[claim], sources=_sources())
    edited = (
        "总编保留模块判断：上级与下级保护可能同时动作，需复核选择性。"
        "[[CLAIM:C-001]]"
    )

    cited = ledger.bind_citations(edited)

    assert cited.endswith("需复核选择性。[[CITE:1]]")

    with pytest.raises(CitationBindingError, match="C-001"):
        ledger.bind_citations("正文没有 Claim 标记。")


def test_source_index_lists_photo_caption_and_all_evidence_bindings() -> None:
    evidence = [
        EvidenceItem(
            id=f"E-00{index}",
            subject="文体中心配电房",
            fact=f"{label}=NG",
            source=SourceLocation(
                file_id="F-1",
                path=Path("Inputs/S4-4诊断工作用表.xlsx"),
                sheet="配电房合规性",
                cell=cell,
            ),
            module_id="2.2",
            submodule_id="2.2.2.3",
            photo_refs=["P-0005"],
        )
        for index, label, cell in (
            (1, "门窗合规", "B6:C6"),
            (2, "出口应急灯", "D6:E6"),
            (3, "防鼠板", "F6:G6"),
        )
    ]
    project_sources = [
        SourceRecord(
            id=item.id,
            kind=SourceKind.PROJECT_EVIDENCE,
            title=item.subject,
            locator=(
                f"{item.source.path.as_posix()}；工作表={item.source.sheet}；"
                f"单元格={item.source.cell}"
            ),
        )
        for item in evidence
    ]
    photo = PhotoAsset(
        id="P-0005",
        path=Path(
            "Work/runs/report-test/assets/file-s44/P-0005.jpeg"
        ),
        sha256="abc",
        media_type="image/jpeg",
        source_member="xl/media/image5.jpeg",
        source_image_id="ID_SOURCE",
        primary_evidence_id="E-001",
    )

    markdown = ClaimLedger(
        claims=[],
        sources=project_sources,
    ).source_index_markdown(
        evidence_items=evidence,
        photo_assets=[photo],
    )

    assert "### 图片证据 P-*" in markdown
    assert "- P-0005：主说明=文体中心配电房：门窗合规=NG" in markdown
    assert "主证据=E-001" in markdown
    assert "E-002（文体中心配电房：出口应急灯=NG）" in markdown
    assert "E-003（文体中心配电房：防鼠板=NG）" in markdown
    assert "原始图片键=ID_SOURCE" in markdown
    assert "文件=Work/runs/report-test/assets/file-s44/P-0005.jpeg" in markdown
