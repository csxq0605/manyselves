from pathlib import Path
from zipfile import ZipFile

from manyselves.core.reporting.intake.wps_images import extract_wps_images


def test_extract_wps_images_follows_cell_image_relationships(tmp_path: Path) -> None:
    workbook_path = tmp_path / "wps.xlsx"
    cell_images = b"""<?xml version="1.0" encoding="UTF-8"?>
    <etc:cellImages
      xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData"
      xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <etc:cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="ID_TEST"/></xdr:nvPicPr>
      <xdr:blipFill><a:blip r:embed="rId7"/></xdr:blipFill></xdr:pic></etc:cellImage>
    </etc:cellImages>"""
    relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId7" Type="image" Target="media/image7.jpeg"/>
    </Relationships>"""
    with ZipFile(workbook_path, "w") as archive:
        archive.writestr("xl/cellimages.xml", cell_images)
        archive.writestr("xl/_rels/cellimages.xml.rels", relationships)
        archive.writestr("xl/media/image7.jpeg", b"fake-jpeg")

    assets = extract_wps_images(workbook_path, output_dir=tmp_path / "assets")

    assert set(assets) == {"ID_TEST"}
    assert assets["ID_TEST"].path.read_bytes() == b"fake-jpeg"
    assert assets["ID_TEST"].source_member == "xl/media/image7.jpeg"
    assert len(assets["ID_TEST"].sha256) == 64


def test_extract_wps_images_returns_empty_mapping_when_extension_is_absent(
    tmp_path: Path,
) -> None:
    workbook_path = tmp_path / "plain.xlsx"
    with ZipFile(workbook_path, "w") as archive:
        archive.writestr("xl/workbook.xml", b"<workbook/>")

    assert extract_wps_images(workbook_path, output_dir=tmp_path / "assets") == {}
