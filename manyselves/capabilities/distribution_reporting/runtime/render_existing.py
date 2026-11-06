"""File-defined, deterministic rendering of an existing Markdown artifact."""

from __future__ import annotations

import io
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Literal

from docx import Document
from pydantic import Field

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportingModel,
    ReportRequest,
)

from ..domain.taxonomy import (
    bound_report_taxonomy,
    report_taxonomy_bound,
    taxonomy_from_markdown,
)
from .delivery_tools import atomic_copy_file
from .input_snapshot import RunInputSnapshotStore
from .rendering.contracts import RenderRequest, RenderResult
from .rendering.handoff_docx import PackagedV2DocxCore
from .rendering.pds_docx_renderer import PdsDocxRenderer
from .rendering.rendered_docx_validator import validate_rendered_markdown_docx
from .run_taxonomy import bind_run_taxonomy
from .storage import ReportingStore
from .template_resolver import resolve_report_template


class RenderExistingRequest(ReportRequest):
    """Public request narrowed to the existing-Markdown rendering operation."""

    operation: Literal["render_existing"] = "render_existing"


class RenderExistingPrepareInput(ReportingModel):
    """Internal tool input combining the request with the Generic Run identity."""

    request: RenderExistingRequest
    run_id: str = Field(min_length=1)


class RenderExistingContext(ReportingModel):
    """Serializable context passed from preparation to deterministic rendering."""

    run_id: str = Field(min_length=1)
    source_markdown_ref: Path
    source_snapshot_ref: Path
    template_ref: Path
    output_ref: Path
    template_source: str = Field(min_length=1)
    render_request_ref: Path
    template_provenance_ref: Path


def _relative_template_path(workspace: Path, selected_template: Path) -> str:
    if selected_template.is_relative_to(workspace):
        return selected_template.relative_to(workspace).as_posix()
    return "manyselves/templates/reporting/report_template.docx"


def prepare_render_existing(
    value: RenderExistingPrepareInput,
    *,
    workspace: Path,
    snapshots: RunInputSnapshotStore,
    store: ReportingStore,
) -> RenderExistingContext:
    """Freeze and materialize the source/template handoff for one Run."""

    request = value.request
    snapshot = snapshots.freeze(
        value.run_id,
        extra_refs=(request.source_markdown_ref,),
    )
    source_snapshot_ref = snapshot.resolve(request.source_markdown_ref)
    selected_template, template_source = resolve_report_template(
        workspace,
        value.run_id,
    )
    template_ref = Path(
        f"Work/runs/{value.run_id}/templates/report_template.docx"
    )
    atomic_copy_file(workspace, selected_template, workspace / template_ref)
    output_ref = Path("Outputs/Reports") / (
        request.output_filename or f"{request.source_markdown_ref.stem}.docx"
    )
    render_request_ref = Path(f"Work/runs/{value.run_id}/render-request.json")
    template_provenance_ref = Path(
        f"Work/runs/{value.run_id}/template-provenance.json"
    )
    render_request = RenderRequest(
        run_id=value.run_id,
        source_markdown_ref=request.source_markdown_ref,
        source_snapshot_ref=source_snapshot_ref,
        template_ref=template_ref,
        output_ref=output_ref,
    )
    store.write_json(
        render_request_ref.as_posix(),
        render_request.model_dump(mode="json"),
    )
    store.write_json(
        template_provenance_ref.as_posix(),
        {
            "source": template_source,
            "selected_path": _relative_template_path(workspace, selected_template),
            "snapshot_path": template_ref.as_posix(),
            "storage": "materialized",
        },
    )
    return RenderExistingContext(
        run_id=value.run_id,
        source_markdown_ref=request.source_markdown_ref,
        source_snapshot_ref=source_snapshot_ref,
        template_ref=template_ref,
        output_ref=output_ref,
        template_source=template_source,
        render_request_ref=render_request_ref,
        template_provenance_ref=template_provenance_ref,
    )


def _render_document(
    value: RenderExistingContext,
    *,
    workspace: Path,
    store: ReportingStore,
) -> RenderResult:
    source_path = workspace / value.source_snapshot_ref
    markdown = source_path.read_text(encoding="utf-8")
    if not markdown.strip():
        raise ValueError("render source Markdown is empty")

    output = workspace / value.output_ref
    template = workspace / value.template_ref
    # A report rendered on its own carries its chapter 2 tree in its headings.
    with (
        nullcontext() if report_taxonomy_bound()
        else bound_report_taxonomy(taxonomy_from_markdown(markdown))
    ):
        _rendered_name, raw_docx = PackagedV2DocxCore(template).render_approved_prose(
            markdown,
            filename=output.name,
            report_model=None,
        )
    title = next(
        (
            line.removeprefix("#").strip()
            for line in markdown.splitlines()
            if line.startswith("# ")
        ),
        "配电安全专家咨询报告",
    )
    rendered_document = Document(io.BytesIO(raw_docx))
    PdsDocxRenderer._ensure_title(rendered_document, title)
    rendered_buffer = io.BytesIO()
    rendered_document.save(rendered_buffer)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.stem}-",
            suffix=".docx",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary.write(rendered_buffer.getvalue())
            temporary_path = Path(temporary.name)
        validation_warnings = validate_rendered_markdown_docx(
            temporary_path,
            markdown,
            expected_title=title,
            reject_unresolved_tokens=True,
        )
        temporary_path.replace(output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    render_log_ref = Path(f"Work/runs/{value.run_id}/render-log.json")
    result = RenderResult(
        status="completed",
        run_id=value.run_id,
        source_markdown_ref=value.source_markdown_ref,
        source_snapshot_ref=value.source_snapshot_ref,
        output_ref=value.output_ref,
        render_log_ref=render_log_ref,
        protected_prose_verified=not validation_warnings,
        validation_warnings=validation_warnings,
    )
    store.write_json(render_log_ref.as_posix(), result.model_dump(mode="json"))
    store.write_json(
        f"Work/runs/{value.run_id}/render-result.json",
        result.model_dump(mode="json"),
    )
    return result


def build_render_existing_tool_implementations(
    workspace: Path,
) -> dict[str, Any]:
    """Bind file-defined Tool IDs to this Capability's deterministic functions."""

    workspace = Path(workspace)
    snapshots = RunInputSnapshotStore(workspace)
    store = ReportingStore(workspace)

    def prepare(value: Any) -> RenderExistingContext:
        return prepare_render_existing(
            RenderExistingPrepareInput.model_validate(value),
            workspace=workspace,
            snapshots=snapshots,
            store=store,
        )

    def render(value: Any) -> RenderResult:
        return _render_document(
            RenderExistingContext.model_validate(value),
            workspace=workspace,
            store=store,
        )

    return bind_run_taxonomy({
        "prepare-render-existing": prepare,
        "render-existing": render,
    }, workspace)


__all__ = [
    "RenderExistingContext",
    "RenderExistingPrepareInput",
    "RenderExistingRequest",
    "build_render_existing_tool_implementations",
    "prepare_render_existing",
]
