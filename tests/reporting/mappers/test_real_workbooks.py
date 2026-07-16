from pathlib import Path

import pytest

from manyselves.core.reporting.intake.wps_images import extract_wps_images
from manyselves.core.reporting.mappers.s2_1 import map_s2_1
from manyselves.core.reporting.mappers.s4_4 import map_s4_4
from manyselves.core.reporting.mappers.s4_6 import map_s4_6

REAL_INPUT_DIR = Path("/Users/zzymima0000/Documents/Codex/work/写作上传材料")
pytestmark = pytest.mark.skipif(
    not REAL_INPUT_DIR.is_dir(),
    reason="local V2 handoff workbooks are not available",
)


def test_real_s2_1_maps_all_collection_rows() -> None:
    result = map_s2_1(REAL_INPUT_DIR / "S2-1收资表.xlsx", file_id="real-s21")

    assert len(result.evidence_items) == 25
    assert any(
        item.submodule_id == "2.4.1.1" and "主要开关" in item.subject
        for item in result.evidence_items
    )


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


def test_real_s4_6_marks_prior_assessment_as_confirmation_required() -> None:
    result = map_s4_6(REAL_INPUT_DIR / "S4-6评估总表.xlsx", file_id="real-s46")

    grounding = [item for item in result.evidence_items if item.submodule_id == "2.4.2.2"]
    assert grounding
    assert all(item.needs_confirmation for item in result.evidence_items)
    assert all(item.confidence < 1 for item in result.evidence_items)
