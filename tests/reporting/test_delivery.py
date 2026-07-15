from pathlib import Path

import pytest
from docx import Document

from autoreport.core.reporting.delivery import DeliveryPackage, ProjectDelivery


def _package(tmp_path: Path) -> DeliveryPackage:
    modules = {}
    for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5"):
        path = tmp_path / f"module-{module_id}.md"
        path.write_text(f"# {module_id}\n\n专业正文", encoding="utf-8")
        modules[module_id] = path
    final = tmp_path / "final.docx"
    Document().save(final)
    state = tmp_path / "report-state.json"
    state.write_text('{"approved": true}', encoding="utf-8")
    return DeliveryPackage(
        report_id="report-001",
        version="v1",
        module_files=modules,
        final_docx=final,
        report_state=state,
    )


def test_delivery_publishes_complete_five_module_package_atomically(tmp_path: Path) -> None:
    delivery = ProjectDelivery(tmp_path / "project" / "Outputs")

    receipt = delivery.deliver(_package(tmp_path))

    assert receipt.success is True
    assert receipt.final_docx.is_file()
    assert set(receipt.module_files) == {"2.1", "2.2", "2.3", "2.4", "2.5"}
    assert Document(receipt.final_docx)
    assert receipt.manifest_path.is_file()


def test_delivery_rejects_incomplete_or_invalid_docx_without_success_receipt(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    package.module_files.pop("2.5")
    delivery_root = tmp_path / "Outputs"

    with pytest.raises(ValueError, match="exactly modules 2.1-2.5"):
        ProjectDelivery(delivery_root).deliver(package)
    assert not delivery_root.exists()

    package = _package(tmp_path)
    package.final_docx.write_text("not a docx", encoding="utf-8")
    with pytest.raises(ValueError, match="Word/WPS-openable"):
        ProjectDelivery(delivery_root).deliver(package)
    assert not delivery_root.exists()
