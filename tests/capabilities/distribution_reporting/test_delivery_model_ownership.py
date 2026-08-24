"""Physical ownership and serialization contracts for Delivery stage models."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib import import_module
from pathlib import Path

DELIVERY_MODEL_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.models.delivery"
)


def _materialized_receipt() -> object:
    delivery = import_module(DELIVERY_MODEL_MODULE)
    delivery_dir = Path("Work/runs/run-delivery/delivery/run-delivery-run")
    return delivery.MaterializedDeliveryReceipt(
        success=True,
        delivery_dir=delivery_dir,
        final_docx=delivery_dir / "report.docx",
        module_files={
            module_id: delivery_dir / "modules" / f"{module_id}.md"
            for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
        },
        report_state=delivery_dir / "report-state.json",
        source_index=delivery_dir / "证据与来源索引.md",
        source_index_docx=delivery_dir / "证据与来源索引.docx",
        manifest_path=delivery_dir / "delivery-manifest.json",
    )


def test_delivery_models_are_capability_owned_and_core_definitions_are_absent() -> None:
    delivery = import_module(DELIVERY_MODEL_MODULE)
    assert delivery.DeliveryContext.__module__ == DELIVERY_MODEL_MODULE
    assert delivery.MaterializedDeliveryReceipt.__module__ == DELIVERY_MODEL_MODULE


def test_delivery_models_import_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    f"import {DELIVERY_MODEL_MODULE} as delivery",
                    "print(json.dumps({",
                    "    'module': delivery.__name__,",
                    "    'core_reporting': sorted(",
                    "        name for name in sys.modules",
                    "        if name.startswith('manyselves.core.reporting')",
                    "    ),",
                    "}))",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "module": DELIVERY_MODEL_MODULE,
        "core_reporting": [],
    }


def test_delivery_models_round_trip_as_typed_json() -> None:
    delivery = import_module(DELIVERY_MODEL_MODULE)
    delivery_dir = Path("Work/runs/run-delivery/delivery/run-delivery-run")
    receipt = _materialized_receipt()
    context = delivery.DeliveryContext(
        state={"run_id": "run-delivery"},
        final_audit_snapshot_ref="Work/runs/run-delivery/final-audit.json",
        claim_ledger_path=Path("Work/runs/run-delivery/claims.json"),
        source_ledger_path=Path("Work/runs/run-delivery/sources.json"),
        evidence_snapshot_path=Path("Work/runs/run-delivery/evidence.jsonl"),
        approved_module_paths={},
        edited_submission_path=Path("Work/runs/run-delivery/edited.json"),
        request_snapshot_path=Path("Work/runs/run-delivery/request.json"),
        photo_manifest_path=Path("Work/runs/run-delivery/photos.json"),
        delivery_markdown="# Report",
        report_state_path=delivery_dir / "report-state.json",
        markdown_path=Path("Work/runs/run-delivery/report.md"),
        source_index_markdown="# Sources",
        source_index_path=delivery_dir / "证据与来源索引.md",
        source_index_docx_path=delivery_dir / "证据与来源索引.docx",
        template_snapshot=Path("Work/runs/run-delivery/template.docx"),
        template_provenance_path=Path("Work/runs/run-delivery/template.json"),
        output=Path("Work/runs/run-delivery/report.docx"),
        render_result_ref=Path("Work/runs/run-delivery/render-result.json"),
        receipt=receipt,
    )

    restored = delivery.DeliveryContext.model_validate(
        context.model_dump(mode="json")
    )

    assert restored == context
    assert isinstance(restored.receipt, delivery.MaterializedDeliveryReceipt)
