"""Column headers and image cells are read from the source workbook layout."""

from pathlib import Path

from openpyxl import Workbook

from manyselves.capabilities.distribution_reporting.runtime.mappers.common import (
    make_evidence,
    source_layout,
)
from manyselves.capabilities.distribution_reporting.runtime.research.fact_context import (
    render_fact_packet,
)


def test_headers_above_the_records_and_image_cells_are_read(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "明细"
    sheet["A1"] = "填写说明"
    sheet.merge_cells("A1:B1")
    sheet["C1"] = "实测数据 >10A附图"
    sheet["A2"], sheet["B2"], sheet["C2"] = "配电房", "柜号", "剩余电流"
    sheet["A3"], sheet["B3"], sheet["C3"] = "一号房", "14#", 11.3
    sheet["D3"] = '=DISPIMG("ID_1",1)'
    path = tmp_path / "t.xlsx"
    workbook.save(path)
    item = make_evidence(
        file_id="f", path=Path("Inputs/t.xlsx"), sheet="明细", cell="C3", row=3,
        column="C", subject="一号房/14#", fact="剩余电流=11.3A", module_id="2.4",
        submodule_id="2.4.1.1", photo_refs=["ID_1"],
    )

    headers, cells = source_layout(path, [item], {"ID_1"})

    assert headers == {
        "明细": {"A": "填写说明 / 配电房", "B": "填写说明 / 柜号", "C": "实测数据 >10A附图 / 剩余电流"}
    }
    assert cells == {"ID_1": ("明细", "D3")}
    packet = render_fact_packet(
        [item], "2.4", column_headers={("Inputs/t.xlsx", "明细"): headers["明细"]}
    )
    assert '"C":"实测数据 >10A附图 / 剩余电流"' in packet
