from pathlib import Path
from zipfile import ZipFile

import pytest

from manyselves.capabilities.distribution_reporting.runtime.intake.wps_images import (
    canonicalize_photo_bindings,
    extract_wps_images,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    PhotoAsset,
    SourceLocation,
)


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
    assert assets["ID_TEST"].source_image_id == "ID_TEST"
    assert len(assets["ID_TEST"].sha256) == 64

    evidence = EvidenceItem(
        id="E-0001",
        subject="测试对象",
        fact="检查结果=NG",
        source=SourceLocation(file_id="F-1", path=workbook_path, cell="A1:B1"),
        module_id="2.4",
        submodule_id="2.4.2.1",
        photo_refs=["ID_TEST"],
    )
    normalized_evidence, normalized_assets = canonicalize_photo_bindings(
        [evidence],
        assets,
        start_index=7,
    )

    assert normalized_evidence[0].photo_refs == ["P-0007"]
    assert normalized_assets[0].id == "P-0007"
    assert normalized_assets[0].path.name == "P-0007.jpeg"
    assert normalized_assets[0].source_image_id == "ID_TEST"
    assert normalized_assets[0].primary_evidence_id == "E-0001"


def test_extract_wps_images_can_ignore_unreferenced_workbook_images(
    tmp_path: Path,
) -> None:
    workbook_path = tmp_path / "wps.xlsx"
    cell_images = b"""<?xml version="1.0" encoding="UTF-8"?>
    <etc:cellImages
      xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData"
      xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <etc:cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="ID_USED"/></xdr:nvPicPr>
      <xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic></etc:cellImage>
      <etc:cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="2" name="ID_DECORATION"/></xdr:nvPicPr>
      <xdr:blipFill><a:blip r:embed="rId2"/></xdr:blipFill></xdr:pic></etc:cellImage>
    </etc:cellImages>"""
    relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="image" Target="media/image1.jpeg"/>
      <Relationship Id="rId2" Type="image" Target="media/image2.jpeg"/>
    </Relationships>"""
    with ZipFile(workbook_path, "w") as archive:
        archive.writestr("xl/cellimages.xml", cell_images)
        archive.writestr("xl/_rels/cellimages.xml.rels", relationships)
        archive.writestr("xl/media/image1.jpeg", b"used")
        archive.writestr("xl/media/image2.jpeg", b"decoration")

    assets = extract_wps_images(
        workbook_path,
        output_dir=tmp_path / "assets",
        required_image_ids={"ID_USED"},
    )

    assert set(assets) == {"ID_USED"}
    assert [path.name for path in (tmp_path / "assets").iterdir()] == [
        "source-0001.jpeg"
    ]


def test_extract_wps_images_returns_empty_mapping_when_extension_is_absent(
    tmp_path: Path,
) -> None:
    workbook_path = tmp_path / "plain.xlsx"
    with ZipFile(workbook_path, "w") as archive:
        archive.writestr("xl/workbook.xml", b"<workbook/>")

    assert extract_wps_images(workbook_path, output_dir=tmp_path / "assets") == {}


def test_canonicalize_photo_bindings_rejects_unextractable_reference() -> None:
    evidence = EvidenceItem(
        id="E-0001",
        subject="测试对象",
        fact="检查结果=NG",
        source=SourceLocation(file_id="F-1", path=Path("Inputs/test.xlsx"), cell="A1"),
        module_id="2.4",
        submodule_id="2.4.2.1",
        photo_refs=["ID_MISSING"],
    )

    with pytest.raises(ValueError, match="unextractable workbook photos"):
        canonicalize_photo_bindings([evidence], {}, start_index=1)


def test_canonicalize_photo_bindings_persists_primary_binding_for_reused_photo(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source-0001.jpeg"
    source_path.write_bytes(b"fake-jpeg")
    asset = PhotoAsset(
        id="ID_SHARED",
        path=source_path,
        sha256="abc",
        media_type="image/jpeg",
        source_member="xl/media/image1.jpeg",
        source_image_id="ID_SHARED",
    )
    evidence = [
        EvidenceItem(
            id=f"E-000{index}",
            subject=f"测试对象 {index}",
            fact="检查结果=NG",
            source=SourceLocation(
                file_id="F-1",
                path=Path("Inputs/test.xlsx"),
                cell=f"A{index}:B{index}",
            ),
            module_id="2.4",
            submodule_id="2.4.2.1",
            photo_refs=["ID_SHARED"],
        )
        for index in (1, 2)
    ]

    normalized_evidence, normalized_assets = canonicalize_photo_bindings(
        evidence,
        {"ID_SHARED": asset},
        start_index=1,
    )

    assert [item.photo_refs for item in normalized_evidence] == [
        ["P-0001"],
        ["P-0001"],
    ]
    assert normalized_assets[0].primary_evidence_id == "E-0001"
    assert not source_path.exists()
    assert normalized_assets[0].path == tmp_path / "P-0001.jpeg"
