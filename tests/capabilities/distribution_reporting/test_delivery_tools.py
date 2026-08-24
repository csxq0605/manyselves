from __future__ import annotations

import json
from pathlib import Path

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.delivery_tools import DeliveryTools
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.delivery import (
    DeliveryContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _module_submissions() -> dict[str, dict]:
    return {
        module_id: ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                submodule_id: f"内容 {submodule_id}"
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            },
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        ).model_dump(mode="json")
        for module_id in REPORT_MODULE_IDS
    }


def test_publish_materialize_is_capability_owned_for_serialized_delivery_context(
    tmp_path: Path,
) -> None:
    run_id = "run-delivery-capability"
    run_root = tmp_path / "Work" / "runs" / run_id
    run_root.mkdir(parents=True)
    report_docx = run_root / "report" / "配电安全专家咨询报告.docx"
    source_index_docx = run_root / "source-index" / "证据与来源索引.docx"
    source_index = run_root / "source-index" / "证据与来源索引.md"
    report_state = run_root / "report-state.json"
    report_docx.parent.mkdir(parents=True)
    source_index.parent.mkdir(parents=True)
    report_docx.write_bytes(b"report docx")
    source_index_docx.write_bytes(b"source index docx")
    source_index.write_text("# Sources\n", encoding="utf-8")
    report_state.write_text("{}\n", encoding="utf-8")

    context = DeliveryContext(
        state={},
        final_audit_snapshot_ref=f"Work/runs/{run_id}/final-audit.json",
        claim_ledger_path=run_root / "claims.json",
        source_ledger_path=run_root / "sources.json",
        evidence_snapshot_path=run_root / "evidence.jsonl",
        approved_module_paths={},
        edited_submission_path=run_root / "edited.json",
        request_snapshot_path=run_root / "request.json",
        photo_manifest_path=run_root / "photos.json",
        delivery_markdown="# Report\n",
        report_state_path=report_state,
        markdown_path=run_root / "report" / "report.md",
        source_index_markdown="# Sources\n",
        source_index_path=source_index,
        source_index_docx_path=source_index_docx,
        template_snapshot=run_root / "template.docx",
        template_provenance_path=run_root / "template.json",
        output=report_docx,
        render_result_ref=run_root / "render-result.json",
    )
    state = json.loads(
        json.dumps(
            {
                "run_id": run_id,
                "module_submissions": _module_submissions(),
                "_declarative_delivery_context": context.model_dump(
                    mode="json", exclude={"state"}
                ),
            }
        )
    )

    published = DeliveryTools(tmp_path, ReportingStore(tmp_path)).publish(state)
    published_context = DeliveryContext.model_validate(
        {
            "state": published,
            **published["_declarative_delivery_context"],
        }
    )

    assert (tmp_path / "Outputs/Reports/配电安全专家咨询报告.md").is_file()
    assert (tmp_path / "Outputs/Reports/配电安全专家咨询报告.docx").is_file()
    assert (tmp_path / "Outputs/Reports/证据与来源索引.md").is_file()
    assert (tmp_path / "Outputs/Reports/证据与来源索引.docx").is_file()
    for module_id in REPORT_MODULE_IDS:
        assert (tmp_path / f"Outputs/Modules/{module_id}.md").is_file()
    assert published_context.receipt is not None
    assert published_context.receipt_path == run_root / "delivery-receipt.json"
    assert published_context.receipt.manifest_path.is_file()


def test_complete_materialized_delivery_is_capability_owned_after_json_roundtrip(
    tmp_path: Path,
) -> None:
    run_id = "run-delivery-complete-capability"
    run_root = tmp_path / "Work" / "runs" / run_id
    run_root.mkdir(parents=True)
    report_docx = run_root / "report" / "配电安全专家咨询报告.docx"
    source_index_docx = run_root / "source-index" / "证据与来源索引.docx"
    source_index = run_root / "source-index" / "证据与来源索引.md"
    report_state = run_root / "report-state.json"
    report_docx.parent.mkdir(parents=True)
    source_index.parent.mkdir(parents=True)
    report_docx.write_bytes(b"report docx")
    source_index_docx.write_bytes(b"source index docx")
    source_index.write_text("# Sources\n", encoding="utf-8")
    report_state.write_text("{}\n", encoding="utf-8")

    context = DeliveryContext(
        state={},
        final_audit_snapshot_ref=(
            f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
        ),
        claim_ledger_path=run_root / "claims.json",
        source_ledger_path=run_root / "sources.json",
        evidence_snapshot_path=run_root / "evidence.jsonl",
        approved_module_paths={},
        edited_submission_path=run_root / "edited.json",
        request_snapshot_path=run_root / "request.json",
        photo_manifest_path=run_root / "photos.json",
        delivery_markdown="# Report\n",
        report_state_path=report_state,
        markdown_path=run_root / "report" / "report.md",
        source_index_markdown="# Sources\n",
        source_index_path=source_index,
        source_index_docx_path=source_index_docx,
        template_snapshot=run_root / "template.docx",
        template_provenance_path=run_root / "template.json",
        output=report_docx,
        render_result_ref=run_root / "render-result.json",
    )
    state = json.loads(
        json.dumps(
            {
                "run_id": run_id,
                "final_review_completion_ref": (
                    f"Work/runs/{run_id}/reviews/final-completion.json"
                ),
                "module_submissions": _module_submissions(),
                "_declarative_delivery_context": context.model_dump(
                    mode="json", exclude={"state"}
                ),
            }
        )
    )

    runtime = DeliveryTools(tmp_path, ReportingStore(tmp_path))
    published = runtime.publish(state)
    published["module_submissions"] = {
        module_id: submission.model_dump(mode="json")
        for module_id, submission in published["module_submissions"].items()
    }
    restored = json.loads(json.dumps(published))
    completed = DeliveryTools(tmp_path, ReportingStore(tmp_path)).complete(restored)

    completion = json.loads(
        (run_root / "delivery-completion.json").read_text(encoding="utf-8")
    )
    assert completed["delivery_status"] == "delivered"
    assert completed["delivery_completion_ref"] == (
        f"Work/runs/{run_id}/delivery-completion.json"
    )
    assert completion["final_audit_snapshot_ref"] == (
        f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
    )
    assert completion["final_review_completion_ref"] == (
        f"Work/runs/{run_id}/reviews/final-completion.json"
    )
    assert "_declarative_delivery_context" not in completed
    assert {
        (artifact["kind"], artifact["path"], artifact["module_id"])
        for artifact in completed["output_artifacts"]
    } == {
        *{
            ("module", f"Outputs/Modules/{module_id}.md", module_id)
            for module_id in REPORT_MODULE_IDS
        },
        (
            "review",
            f"Work/runs/{run_id}/reviews/final-completion.json",
            None,
        ),
        ("report", "Outputs/Reports/配电安全专家咨询报告.md", None),
        ("report", "Outputs/Reports/配电安全专家咨询报告.docx", None),
        ("report", "Outputs/Reports/证据与来源索引.md", None),
        ("report", "Outputs/Reports/证据与来源索引.docx", None),
        (
            "run",
            f"Work/runs/{run_id}/delivery/{run_id}-{run_id}/delivery-manifest.json",
            None,
        ),
    }
    assert "report_version_id" not in completion
    assert "version_warning" not in completion
    assert not (tmp_path / "Work" / "report-versions").exists()
    assert "report_version" not in completed
