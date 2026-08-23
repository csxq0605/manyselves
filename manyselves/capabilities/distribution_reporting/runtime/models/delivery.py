"""Typed carriers for the distribution-reporting Delivery stages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from .agentic import StrictModel


class MaterializedDeliveryReceipt(StrictModel):
    """Current delivery contract: ordinary files and no digest/CAS identity."""

    success: bool
    delivery_dir: Path
    final_docx: Path
    module_files: dict[str, Path]
    report_state: Path
    source_index: Path
    source_index_docx: Path
    manifest_path: Path

    @model_validator(mode="after")
    def source_indexes_are_delivery_views(self) -> "MaterializedDeliveryReceipt":
        delivery_dir = Path(self.delivery_dir)
        if (
            Path(self.source_index) != delivery_dir / "证据与来源索引.md"
            or Path(self.source_index_docx) != delivery_dir / "证据与来源索引.docx"
        ):
            raise ValueError("source index paths must be current delivery view paths")
        return self


class DeliveryContext(BaseModel):
    """Capability-owned values passed through the synchronous Delivery stages."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # The workflow state remains the runner-owned mutable mapping.  Keeping it
    # opaque here preserves the existing in-place completion updates while the
    # delivery artifacts crossing stage boundaries stay typed.
    state: Any
    final_audit_snapshot_ref: str
    claim_ledger_path: Path
    source_ledger_path: Path
    evidence_snapshot_path: Path
    approved_module_paths: dict[str, Path]
    edited_submission_path: Path
    request_snapshot_path: Path
    photo_manifest_path: Path
    delivery_markdown: str
    report_state_path: Path
    markdown_path: Path
    source_index_markdown: str
    source_index_path: Path
    source_index_docx_path: Path
    template_snapshot: Path
    template_provenance_path: Path
    output: Path
    render_result_ref: Path
    public_markdown: Path | None = None
    public_docx: Path | None = None
    public_source_index: Path | None = None
    public_source_index_docx: Path | None = None
    receipt: MaterializedDeliveryReceipt | None = None
    receipt_path: Path | None = None


__all__ = ["DeliveryContext", "MaterializedDeliveryReceipt"]
