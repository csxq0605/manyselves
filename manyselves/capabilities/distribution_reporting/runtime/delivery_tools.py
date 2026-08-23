"""Capability-owned ordinary-file publish and delivery materialization."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models.agentic import ModuleSubmission
from .models.delivery import DeliveryContext, MaterializedDeliveryReceipt
from .models.reporting import REPORT_MODULE_IDS
from .storage import ReportingStore

_DELIVERY_CONTEXT_KEY = "_declarative_delivery_context"


def delivery_root(workspace: Path, run_id: str) -> Path:
    """Keep materialized delivery files inside their owning run."""

    return Path(workspace) / "Work" / "runs" / run_id / "delivery"


def atomic_copy_file(workspace: Path, source: Path, target: Path) -> Path:
    """Copy ordinary file bytes atomically beneath the active workspace."""

    source = Path(source)
    target = Path(target)
    if not source.is_file():
        raise FileNotFoundError(f"delivery source is missing: {source}")
    target_root = target.parent.resolve()
    workspace_root = Path(workspace).resolve()
    if not target_root.is_relative_to(workspace_root):
        raise ValueError(f"delivery target is outside the workspace: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as handle:
        with source.open("rb") as source_handle:
            shutil.copyfileobj(source_handle, handle)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


class DeliveryTools:
    """Publish and materialize Delivery values using ordinary project files."""

    def __init__(self, workspace: Path, store: ReportingStore) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store

    def publish(self, state: dict[str, Any]) -> dict[str, Any]:
        """Publish the serialized DeliveryContext carried by the Run state."""

        modules = state.get("module_submissions")
        if isinstance(modules, Mapping):
            state["module_submissions"] = {
                module_id: ModuleSubmission.model_validate(value)
                for module_id, value in modules.items()
            }
        context = DeliveryContext.model_validate(
            {
                "state": state,
                **state[_DELIVERY_CONTEXT_KEY],
            }
        )
        published = self.publish_context(context)
        state[_DELIVERY_CONTEXT_KEY] = published.model_dump(
            mode="json",
            exclude={"state"},
        )
        return state

    def publish_context(self, context: DeliveryContext) -> DeliveryContext:
        """Publish one typed context and materialize its current-run package."""

        state = context.state
        delivery_markdown = context.delivery_markdown
        report_state_path = context.report_state_path
        source_index_path = context.source_index_path
        source_index_docx_path = context.source_index_docx_path
        output = context.output

        public_markdown = self.store.write_text(
            "Outputs/Reports/配电安全专家咨询报告.md",
            delivery_markdown,
        )
        public_docx = atomic_copy_file(
            self.workspace,
            output,
            self.workspace / "Outputs/Reports/配电安全专家咨询报告.docx",
        )
        public_source_index = self.store.write_text(
            "Outputs/Reports/证据与来源索引.md",
            context.source_index_markdown.rstrip() + "\n",
        )
        public_source_index_docx = atomic_copy_file(
            self.workspace,
            source_index_docx_path,
            self.workspace / "Outputs/Reports/证据与来源索引.docx",
        )
        for module_id in REPORT_MODULE_IDS:
            self.store.write_text(
                f"Outputs/Modules/{module_id}.md",
                state["module_submissions"][module_id].markdown,
            )
        receipt = self.materialize_delivery_package(
            state,
            output=output,
            report_state_path=report_state_path,
            source_index_path=source_index_path,
            source_index_docx_path=source_index_docx_path,
        )
        receipt_path = self.store.write_json(
            f"Work/runs/{state['run_id']}/delivery-receipt.json",
            receipt.model_dump(mode="json"),
        )
        return context.model_copy(
            update={
                "public_markdown": public_markdown,
                "public_docx": public_docx,
                "public_source_index": public_source_index,
                "public_source_index_docx": public_source_index_docx,
                "receipt": receipt,
                "receipt_path": receipt_path,
            }
        )

    def materialize_delivery_package(
        self,
        state: dict[str, Any],
        *,
        output: Path,
        report_state_path: Path,
        source_index_path: Path,
        source_index_docx_path: Path,
    ) -> MaterializedDeliveryReceipt:
        """Write the current-run package as ordinary files."""

        run_id = str(state["run_id"])
        delivery_dir = delivery_root(self.workspace, run_id) / f"{run_id}-{run_id}"
        module_files = {
            module_id: self.store.write_text(
                (delivery_dir / "modules" / f"{module_id}.md")
                .relative_to(self.workspace)
                .as_posix(),
                state["module_submissions"][module_id].markdown,
            )
            for module_id in REPORT_MODULE_IDS
        }
        final_docx = atomic_copy_file(
            self.workspace,
            output,
            delivery_dir / "配电安全专家咨询报告.docx",
        )
        report_state = atomic_copy_file(
            self.workspace,
            report_state_path,
            delivery_dir / "report-state.json",
        )
        source_index = self.store.write_text(
            (delivery_dir / "证据与来源索引.md")
            .relative_to(self.workspace)
            .as_posix(),
            Path(source_index_path).read_text(encoding="utf-8"),
        )
        source_index_docx = atomic_copy_file(
            self.workspace,
            source_index_docx_path,
            delivery_dir / "证据与来源索引.docx",
        )
        manifest_path = self.store.write_json(
            (delivery_dir / "delivery-manifest.json")
            .relative_to(self.workspace)
            .as_posix(),
            {
                "manifest_version": 1,
                "report_id": run_id,
                "version": run_id,
                "status": "success",
                "modules": list(REPORT_MODULE_IDS),
                "storage": "materialized",
            },
        )
        return MaterializedDeliveryReceipt(
            success=True,
            delivery_dir=delivery_dir,
            final_docx=final_docx,
            module_files=module_files,
            report_state=report_state,
            source_index=source_index,
            source_index_docx=source_index_docx,
            manifest_path=manifest_path,
        )


def build_delivery_tools(*, workspace: Path, store: ReportingStore) -> DeliveryTools:
    """Construct the Delivery Tool implementation bundle."""

    return DeliveryTools(workspace, store)


def build_delivery_tool_implementations(
    *,
    workspace: Path,
    store: ReportingStore,
) -> dict[str, Any]:
    """Bind the file-declared publish/materialize Tool implementation."""

    tools = build_delivery_tools(workspace=workspace, store=store)
    return {"publish-materialize-delivery": tools.publish}


__all__ = [
    "DeliveryTools",
    "atomic_copy_file",
    "build_delivery_tool_implementations",
    "build_delivery_tools",
    "delivery_root",
]
