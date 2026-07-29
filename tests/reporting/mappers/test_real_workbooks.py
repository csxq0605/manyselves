from pathlib import Path

import pytest

from manyselves.core.reporting.intake.wps_images import extract_wps_images
from manyselves.core.reporting.mappers.s2_1 import map_s2_1
from manyselves.core.reporting.mappers.s4_4 import map_s4_4
from manyselves.core.reporting.mappers.s4_6 import map_s4_6

REAL_INPUT_DIR = Path("/Users/zzymima0000/Documents/Codex/work/写作上传材料")
requires_v2_handoff_workbooks = pytest.mark.skipif(
    not REAL_INPUT_DIR.is_dir(),
    reason="local V2 handoff workbooks are not available",
)
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]


@requires_v2_handoff_workbooks
def test_real_s2_1_maps_all_collection_rows() -> None:
    result = map_s2_1(REAL_INPUT_DIR / "S2-1收资表.xlsx", file_id="real-s21")

    assert len(result.evidence_items) == 25
    assert any(
        item.submodule_id == "2.4.1.1" and "主要开关" in item.subject
        for item in result.evidence_items
    )


@requires_v2_handoff_workbooks
def test_real_s4_4_keeps_measurement_photo_and_row_traceability(tmp_path: Path) -> None:
    path = REAL_INPUT_DIR / "S4-4诊断工作用表.xlsx"
    result = map_s4_4(path, file_id="real-s44")
    assets = extract_wps_images(path, output_dir=tmp_path / "assets")

    cable = next(
        item
        for item in result.evidence_items
        if item.subject == "车间配电房/1A2" and item.source.cell == "H5:I5"
    )
    load_rate = next(
        item
        for item in result.evidence_items
        if item.subject == "车间配电房/2A2" and item.source.cell == "C6:E6"
    )

    assert cable.photo_refs == ["ID_14429E8F9A92405D9C315CA2B13235D2"]
    assert set(cable.photo_refs) <= set(assets)
    assert load_rate.value == pytest.approx(96.992)
    assert "过载" not in load_rate.fact


@requires_v2_handoff_workbooks
def test_real_s4_6_marks_prior_assessment_as_confirmation_required() -> None:
    result = map_s4_6(REAL_INPUT_DIR / "S4-6评估总表.xlsx", file_id="real-s46")

    grounding = [item for item in result.evidence_items if item.submodule_id == "2.4.2.2"]
    assert grounding
    assert all(item.needs_confirmation for item in result.evidence_items)
    assert all(item.confidence < 1 for item in result.evidence_items)


@pytest.mark.parametrize(
    ("scenario", "expected_evidence", "expected_gaps"),
    (("success", 316, 7), ("test", 240, 6)),
)
def test_workspace_input_scenarios_have_no_silently_unrouted_evidence(
    scenario: str,
    expected_evidence: int,
    expected_gaps: int,
) -> None:
    input_dir = WORKSPACE_ROOT / scenario / "Inputs"
    if not input_dir.is_dir():
        pytest.skip(f"{scenario}/Inputs is not available")

    results = [
        map_s2_1(input_dir / "S2-1收资表.xlsx", file_id=f"{scenario}-s21"),
        map_s4_4(input_dir / "S4-4诊断工作用表.xlsx", file_id=f"{scenario}-s44"),
        map_s4_6(input_dir / "S4-6评估总表.xlsx", file_id=f"{scenario}-s46"),
    ]
    evidence = [item for result in results for item in result.evidence_items]
    gaps = [gap for result in results for gap in result.gaps]

    assert len(evidence) == expected_evidence
    assert len(gaps) == expected_gaps
    assert all(item.module_id and item.submodule_id for item in evidence)
    assert not any(gap.code.startswith("unrouted") for gap in gaps)


def test_workspace_test_scenario_preserves_formula_capacity_and_parent_path_rows() -> None:
    input_dir = WORKSPACE_ROOT / "test" / "Inputs"
    if not input_dir.is_dir():
        pytest.skip("test/Inputs is not available")

    s4_4 = map_s4_4(
        input_dir / "S4-4诊断工作用表.xlsx",
        file_id="test-s44",
    )
    capacity = [
        item
        for item in s4_4.evidence_items
        if item.source.sheet == "总配评估详情表"
        and item.source.cell == "C3:E3"
        and item.value == 6250
    ]
    assert {(item.module_id, item.submodule_id) for item in capacity} == {
        ("2.1", "2.1.1"),
        ("2.4", "2.4.1.1"),
    }
    assert {
        (gap.module_id, gap.submodule_id)
        for gap in s4_4.gaps
        if gap.code == "missing_current_for_load_rate"
    } == {("2.1", "2.1.1"), ("2.4", "2.4.1.1")}

    s4_6 = map_s4_6(
        input_dir / "S4-6评估总表.xlsx",
        file_id="test-s46",
    )
    assert any(
        item.source.cell == "C6:E6" and item.submodule_id == "2.1.1"
        for item in s4_6.evidence_items
    )
    assert any(
        item.source.cell == "C37:E37" and item.submodule_id == "2.4.1.1"
        for item in s4_6.evidence_items
    )
    assert any(
        item.source.cell == "C31:E31" and item.submodule_id == "2.4.3.1"
        for item in s4_6.evidence_items
    )
    assert any(
        item.source.cell == "C34:E34" and item.submodule_id == "2.4.2.1"
        for item in s4_6.evidence_items
    )
