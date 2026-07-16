from pathlib import Path

import pytest

from manyselves.core.reporting.agentic_models import (
    ClaimRecord,
    SourceKind,
    SourceRecord,
)
from manyselves.core.reporting.claim_ledger import CitationBindingError, ClaimLedger
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY


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


def test_citation_binding_survives_chief_editor_rewrite_via_explicit_anchor() -> None:
    claim = ClaimRecord(
        id="C-001",
        module_id="2.3",
        submodule_id="2.3.1",
        text="原模块判断",
        claim_type="risk_judgment",
        source_ids=["E-001", "R-001"],
    )
    ledger = ClaimLedger(claims=[claim], sources=_sources())
    edited = "总编改写后指出：上级与下级保护可能同时动作，需复核选择性。"

    cited = ledger.bind_citations(
        edited,
        anchors={"C-001": "需复核选择性"},
    )

    assert cited == "总编改写后指出：上级与下级保护可能同时动作，需复核选择性[[CITE:1]]。"

    with pytest.raises(CitationBindingError, match="C-001"):
        ledger.bind_citations(edited, anchors={"C-001": "不存在的语义锚点"})
