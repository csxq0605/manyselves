"""Capability-owned preparation, rendering, and ordinary-file delivery Tools."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from manyselves.capabilities.distribution_reporting.domain.claim_ledger import ClaimLedger

from .delivery_projection import build_delivery_projection
from .handoff_contracts import write_handoff_contracts
from .models.agentic import EditedReportSubmission, ModuleSubmission
from .models.delivery import DeliveryContext, MaterializedDeliveryReceipt
from .models.reporting import (
    REPORT_MODULE_IDS,
    EvidenceItem,
    OutputArtifact,
    PhotoAsset,
    ReportRequest,
    SpecialTopicPlan,
)
from .rendering.contracts import RenderRequest, RenderResult
from .rendering.handoff_docx import PackagedV2DocxCore
from .rendering.pds_docx_renderer import PdsDocxRenderer
from .rendering.source_index_docx_renderer import SourceIndexDocxRenderer
from .report_validation import validate_final_report_structure
from .source_ledger import SourceLedger
from .storage import ReportingStore
from .template_resolver import resolve_report_template

_DELIVERY_CONTEXT_KEY = "_declarative_delivery_context"


def _restore_delivery_state(state: dict[str, Any]) -> None:
    """Restore the typed values consumed by Capability Delivery Tools."""

    modules = state.get("module_submissions")
    if isinstance(modules, Mapping):
        state["module_submissions"] = {
            module_id: ModuleSubmission.model_validate(value)
            for module_id, value in modules.items()
        }
    request = state.get("request")
    if request is not None:
        state["request"] = ReportRequest.model_validate(request)
    evidence = state.get("evidence_items")
    if isinstance(evidence, list):
        state["evidence_items"] = [
            EvidenceItem.model_validate(item) for item in evidence
        ]
    photos = state.get("photo_assets")
    if isinstance(photos, list):
        state["photo_assets"] = [PhotoAsset.model_validate(item) for item in photos]
    edited = state.get("edited_report")
    if edited is not None:
        state["edited_report"] = EditedReportSubmission.model_validate(edited)
    plan = state.get("special_topic_plan")
    if isinstance(plan, Mapping):
        state["special_topic_plan"] = SpecialTopicPlan.model_validate(plan)


@dataclass(frozen=True, slots=True)
class _DeliveryPreparationDependencies:
    """Narrow Reporting callbacks needed by the Capability preparation Tool."""

    validated_final_audit_subject: Callable[
        [dict[str, Any]], tuple[EditedReportSubmission, str]
    ]


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

    def __init__(
        self,
        workspace: Path,
        store: ReportingStore,
        *,
        preparation: _DeliveryPreparationDependencies | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store
        self.preparation = preparation

    def prepare(self, state: dict[str, Any]) -> dict[str, Any]:
        """Prepare and render the current run through Capability-owned Tools."""

        if self.preparation is None:
            raise RuntimeError("Delivery prepare Tool requires Reporting callbacks")
        _restore_delivery_state(state)
        context = self._prepare_context(state)
        state[_DELIVERY_CONTEXT_KEY] = context.model_dump(
            mode="json",
            exclude={"state"},
        )
        return state

    def _prepare_context(self, state: dict[str, Any]) -> DeliveryContext:
        preparation = cast(_DeliveryPreparationDependencies, self.preparation)
        edited, final_audit_snapshot_ref = preparation.validated_final_audit_subject(
            state
        )
        write_handoff_contracts(self.store, state)
        state["edited_report"] = edited
        claims = [
            claim
            for module_id in REPORT_MODULE_IDS
            for claim in state["module_submissions"][module_id].claims
        ]
        ledger = ClaimLedger(
            claims=claims,
            sources=SourceLedger(self.workspace, state["run_id"]).records,
        )
        claim_ledger_path = self.store.write_json(
            f"Work/runs/{state['run_id']}/ledgers/claims.json",
            ledger.model_dump(mode="json"),
        )
        source_ledger_path = self.store.write_json(
            f"Work/runs/{state['run_id']}/ledgers/sources.json",
            [source.model_dump(mode="json") for source in ledger.sources],
        )
        evidence_snapshot_path = self.store.write_jsonl(
            f"Work/runs/{state['run_id']}/evidence.jsonl",
            [item.model_dump(mode="json") for item in state.get("evidence_items", [])],
        )
        approved_module_paths = {
            module_id: self.store.write_json(
                f"Work/runs/{state['run_id']}/approved-modules/{module_id}.json",
                state["module_submissions"][module_id].model_dump(mode="json"),
            )
            for module_id in REPORT_MODULE_IDS
        }
        edited_submission_path = self.store.write_json(
            f"Work/runs/{state['run_id']}/edited-submission.json",
            edited.model_dump(mode="json"),
        )
        request_snapshot_path = self.store.write_json(
            f"Work/runs/{state['run_id']}/request-snapshot.json",
            state["request"].model_dump(mode="json"),
        )
        photo_manifest_path = self.store.write_json(
            f"Work/runs/{state['run_id']}/photo-manifest.json",
            {
                "assets": [
                    asset.model_dump(mode="json")
                    for asset in state.get("photo_assets", [])
                ]
            },
        )
        report, delivery_markdown = build_delivery_projection(
            self.workspace,
            state,
            edited,
            claims,
        )
        report_state_path = self.store.write_json(
            f"Work/runs/{state['run_id']}/report-state.json",
            report.model_dump(mode="json"),
        )
        validate_final_report_structure(
            store=self.store,
            state=state,
            markdown=delivery_markdown,
            phase="delivery-final",
        )
        markdown_path = self.store.write_text(
            f"Work/runs/{state['run_id']}/report/配电安全专家咨询报告.md",
            delivery_markdown,
        )
        source_index_markdown = ledger.source_index_markdown(
            evidence_items=state.get("evidence_items", []),
            photo_assets=state.get("photo_assets", []),
        )
        source_index_path = self.store.write_text(
            f"Work/runs/{state['run_id']}/source-index/证据与来源索引.md",
            source_index_markdown.rstrip() + "\n",
        )
        source_index_docx_path = (
            self.workspace
            / f"Work/runs/{state['run_id']}/source-index/证据与来源索引.docx"
        )
        SourceIndexDocxRenderer.render(
            source_index_markdown.rstrip() + "\n",
            source_index_docx_path,
        )
        selected_template, template_source = resolve_report_template(
            self.workspace,
            state["run_id"],
        )
        template_snapshot = (
            self.workspace
            / f"Work/runs/{state['run_id']}/templates/report_template.docx"
        )
        atomic_copy_file(
            self.workspace,
            selected_template,
            template_snapshot,
        )
        selected_template_ref = (
            selected_template.relative_to(self.workspace).as_posix()
            if selected_template.is_relative_to(self.workspace)
            else "manyselves/templates/reporting/report_template.docx"
        )
        template_provenance_path = self.store.write_json(
            f"Work/runs/{state['run_id']}/template-provenance.json",
            {
                "source": template_source,
                "selected_path": selected_template_ref,
                "snapshot_path": template_snapshot.relative_to(
                    self.workspace
                ).as_posix(),
                "storage": "materialized",
            },
        )
        output = (
            self.workspace
            / f"Work/runs/{state['run_id']}/report/配电安全专家咨询报告.docx"
        )
        render_request = RenderRequest(
            run_id=state["run_id"],
            source_markdown_ref=markdown_path.relative_to(self.workspace),
            template_ref=template_snapshot.relative_to(self.workspace),
            output_ref=output.relative_to(self.workspace),
        )
        self.store.write_json(
            f"Work/runs/{state['run_id']}/render-request.json",
            render_request.model_dump(mode="json"),
        )
        render = PdsDocxRenderer(PackagedV2DocxCore(template_snapshot)).render(
            report,
            output,
            approved_markdown=delivery_markdown,
        )
        render_log = render.model_dump(mode="json")
        render_log["template"] = {
            "source": template_source,
            "selected_path": selected_template_ref,
        }
        render_log_ref = Path(f"Work/runs/{state['run_id']}/render-log.json")
        self.store.write_json(render_log_ref.as_posix(), render_log)
        render_result_ref = Path(f"Work/runs/{state['run_id']}/render-result.json")
        self.store.write_json(
            render_result_ref.as_posix(),
            RenderResult(
                status="completed",
                run_id=state["run_id"],
                source_markdown_ref=markdown_path.relative_to(self.workspace),
                output_ref=output.relative_to(self.workspace),
                render_log_ref=render_log_ref,
                protected_prose_verified=True,
            ).model_dump(mode="json"),
        )
        return DeliveryContext(
            state=state,
            final_audit_snapshot_ref=final_audit_snapshot_ref,
            claim_ledger_path=claim_ledger_path,
            source_ledger_path=source_ledger_path,
            evidence_snapshot_path=evidence_snapshot_path,
            approved_module_paths=approved_module_paths,
            edited_submission_path=edited_submission_path,
            request_snapshot_path=request_snapshot_path,
            photo_manifest_path=photo_manifest_path,
            delivery_markdown=delivery_markdown,
            report_state_path=report_state_path,
            markdown_path=markdown_path,
            source_index_markdown=source_index_markdown,
            source_index_path=source_index_path,
            source_index_docx_path=source_index_docx_path,
            template_snapshot=template_snapshot,
            template_provenance_path=template_provenance_path,
            output=output,
            render_result_ref=render_result_ref,
        )

    def publish(self, state: dict[str, Any]) -> dict[str, Any]:
        """Publish the serialized DeliveryContext carried by the Run state."""

        _restore_delivery_state(state)
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

    def complete(self, state: dict[str, Any]) -> dict[str, Any]:
        """Complete a published current-run Delivery through ordinary files."""

        _restore_delivery_state(state)
        context = DeliveryContext.model_validate(
            {
                "state": state,
                **state[_DELIVERY_CONTEXT_KEY],
            }
        )
        self.complete_context(context)
        state.pop(_DELIVERY_CONTEXT_KEY, None)
        return state

    def complete_context(self, context: DeliveryContext) -> DeliveryContext:
        """Write the current-run completion projection without version storage."""

        state = context.state
        receipt = cast(MaterializedDeliveryReceipt, context.receipt)
        receipt_path = cast(Path, context.receipt_path)
        public_markdown = cast(Path, context.public_markdown)
        public_docx = cast(Path, context.public_docx)
        public_source_index = cast(Path, context.public_source_index)
        public_source_index_docx = cast(Path, context.public_source_index_docx)
        final_review_ref = str(state["final_review_completion_ref"])
        delivery_manifest_ref = receipt.manifest_path.relative_to(self.workspace)
        state["output_artifacts"] = self._delivery_output_artifacts(
            final_review_ref=final_review_ref,
            delivery_manifest_ref=delivery_manifest_ref,
            final_markdown_ref=public_markdown.relative_to(self.workspace),
            final_docx_ref=public_docx.relative_to(self.workspace),
            source_index_ref=public_source_index.relative_to(self.workspace),
            source_index_docx_ref=public_source_index_docx.relative_to(
                self.workspace
            ),
        )
        completion_ref = Path(
            f"Work/runs/{state['run_id']}/delivery-completion.json"
        )
        state["delivery_status"] = "delivered"
        self.store.write_json(
            completion_ref.as_posix(),
            {
                "run_id": state["run_id"],
                "status": "delivered",
                "delivery_status": "delivered",
                "delivery_receipt_ref": receipt_path.relative_to(
                    self.workspace
                ).as_posix(),
                "final_audit_snapshot_ref": context.final_audit_snapshot_ref,
                "final_review_completion_ref": final_review_ref,
                "output_artifacts": [
                    artifact.model_dump(mode="json")
                    for artifact in state["output_artifacts"]
                ],
            },
        )
        state["delivery_completion_ref"] = completion_ref.as_posix()
        return context

    @staticmethod
    def _delivery_output_artifacts(
        *,
        final_review_ref: str,
        delivery_manifest_ref: Path,
        final_markdown_ref: Path,
        final_docx_ref: Path,
        source_index_ref: Path,
        source_index_docx_ref: Path,
    ) -> list[OutputArtifact]:
        """Declare only artifacts created by the ordinary-file Delivery."""

        return [
            *(
                OutputArtifact(
                    kind="module",
                    path=Path(f"Outputs/Modules/{module_id}.md"),
                    module_id=module_id,
                )
                for module_id in REPORT_MODULE_IDS
            ),
            OutputArtifact(kind="review", path=Path(final_review_ref)),
            OutputArtifact(kind="report", path=final_markdown_ref),
            OutputArtifact(kind="report", path=final_docx_ref),
            OutputArtifact(kind="report", path=source_index_ref),
            OutputArtifact(kind="report", path=source_index_docx_ref),
            OutputArtifact(kind="run", path=delivery_manifest_ref),
        ]

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


def build_delivery_tools(
    *,
    workspace: Path,
    store: ReportingStore,
    preparation: _DeliveryPreparationDependencies | None = None,
) -> DeliveryTools:
    """Construct the Delivery Tool implementation bundle."""

    return DeliveryTools(workspace, store, preparation=preparation)


def build_delivery_tool_implementations(
    *,
    workspace: Path,
    store: ReportingStore,
    preparation: _DeliveryPreparationDependencies | None = None,
) -> dict[str, Any]:
    """Bind the file-declared Delivery Tool implementations."""

    tools = build_delivery_tools(
        workspace=workspace,
        store=store,
        preparation=preparation,
    )
    implementations = {
        "publish-materialize-delivery": tools.publish,
    }
    if preparation is not None:
        implementations["prepare-render-delivery"] = tools.prepare
    implementations["complete-delivery"] = tools.complete
    return implementations


__all__ = [
    "DeliveryTools",
    "atomic_copy_file",
    "build_delivery_tool_implementations",
    "build_delivery_tools",
    "delivery_root",
]
