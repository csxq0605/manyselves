from pathlib import Path

from docx import Document
from openpyxl import Workbook

from manyselves.runtime.artifacts import parse_artifact


def test_docx_tables_and_xlsx_cells_are_explicit_blocks(tmp_path: Path) -> None:
    doc = Document()
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "A"
    table.cell(0, 1).text = "B"
    docx = tmp_path / "sample.docx"
    doc.save(docx)
    assert any("A\tB" in block.text for block in parse_artifact(docx).blocks)

    workbook = Workbook()
    workbook.active["A1"] = "value"
    xlsx = tmp_path / "sample.xlsx"
    workbook.save(xlsx)
    assert parse_artifact(xlsx).blocks[0].text == "value"


def test_image_metadata_does_not_claim_visual_verification(tmp_path: Path) -> None:
    from PIL import Image

    image = tmp_path / "sample.png"
    Image.new("RGB", (10, 20)).save(image)
    parsed = parse_artifact(image)
    assert parsed.kind == "image_metadata"
    assert parsed.visual_verified is False
