from __future__ import annotations

import json
from pathlib import Path

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
    compose_module_markdown,
)
from manyselves.capabilities.distribution_reporting.runtime.delivery_tools import DeliveryTools
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ClaimRecord,
    EditedReportSubmission,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    EvidenceItem,
    ReportRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _edited_report() -> EditedReportSubmission:
    return EditedReportSubmission(
        title="Report",
        assessment_background="background",
        findings_overview="overview",
        regional_executive_summary="summary",
        module_narratives={
            module_id: compose_module_markdown(
                module_id,
                {
                    submodule_id: f"module {module_id} {submodule_id}"
                    for submodule_id in definition.submodules
                },
            )
            for module_id, definition in REPORT_TAXONOMY.items()
        },
        risk_panorama="panorama",
        dimension_risk_analysis="risk analysis",
        data_gap_analysis="gaps",
        improvement_action_plan="actions",
    )


def _module_submissions() -> dict[str, ModuleSubmission]:
    return {
        module_id: ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                submodule_id: f"内容 {submodule_id}"
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            },
            claims=[
                ClaimRecord(
                    id=f"C-{module_id.replace('.', '')}",
                    module_id=module_id,
                    submodule_id=next(iter(REPORT_TAXONOMY[module_id].submodules)),
                    text=f"claim {module_id}",
                    claim_type="recommendation",
                    unresolved=True,
                )
            ],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )
        for module_id in REPORT_MODULE_IDS
    }


def test_delivery_prepare_uses_capability_tool_and_writes_run_scoped_artifacts(
    tmp_path: Path,
) -> None:
    run_id = "run-delivery-prepare"
    store = ReportingStore(tmp_path)
    edited = _edited_report()

    state = {
        "run_id": run_id,
        "request": ReportRequest(instruction="Render the accepted report."),
        "module_submissions": _module_submissions(),
        "evidence_items": [],
        "photo_assets": [],
        "edited_report": edited,
    }
    subject_ref = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
    completion_ref = f"Work/runs/{run_id}/reviews/final-completion.json"
    store.write_json(subject_ref, edited.model_dump(mode="json"))
    store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="final",
            run_id=run_id,
            reviewer_agent_id="chief-editor-auditor",
            reviewer_session_key="chief-editor-auditor",
            subject_refs=[subject_ref],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )
    serialized = json.loads(
        json.dumps(
            {
                "run_id": state["run_id"],
                "final_review_completion_ref": completion_ref,
                "request": state["request"].model_dump(mode="json"),
                "module_submissions": {
                    module_id: value.model_dump(mode="json")
                    for module_id, value in state["module_submissions"].items()
                },
                "evidence_items": [
                    EvidenceItem(
                        id="evidence-delivery-prepare",
                        subject="serialized evidence",
                        fact="the preparation input is restored by the Capability",
                        source={"file_id": "source-delivery-prepare", "path": "Inputs/source.md"},
                    ).model_dump(mode="json")
                ],
                    "photo_assets": [],
                "edited_report": edited.model_dump(mode="json"),
            }
        )
    )
    prepared = DeliveryTools(tmp_path, store).prepare(serialized)

    run_root = tmp_path / "Work" / "runs" / run_id
    expected_files = (
        "ledgers/claims.json",
        "ledgers/sources.json",
        "evidence.jsonl",
        "edited-submission.json",
        "request-snapshot.json",
        "photo-manifest.json",
        "handoff-contracts.json",
        "report-state.json",
        "report/配电安全专家咨询报告.md",
        "report/配电安全专家咨询报告.docx",
        "source-index/证据与来源索引.md",
        "source-index/证据与来源索引.docx",
        "template-provenance.json",
        "render-request.json",
        "render-result.json",
        "render-log.json",
    )
    assert all((run_root / relative).is_file() for relative in expected_files)
    assert all(
        (run_root / "approved-modules" / f"{module_id}.json").is_file()
        for module_id in REPORT_MODULE_IDS
    )
    legacy_snapshot = json.loads(
        (run_root / "reviews/final-audit-snapshot.json").read_text(encoding="utf-8")
    )
    assert legacy_snapshot == {
        "kind": "final_audit_snapshot",
        "run_id": run_id,
        "subject_ref": subject_ref,
        "subject_revision": 0,
        "canonical_markdown_ref": (
            f"Work/runs/{run_id}/validation/report-final-audit-legacy.md"
        ),
        "validation_report_ref": (
            f"Work/runs/{run_id}/reviews/report-integrity-final-audit-legacy.json"
        ),
        "completion_ref": completion_ref,
    }
    assert (run_root / "validation/report-final-audit-legacy.md").is_file()
    legacy_validation = json.loads(
        (run_root / "reviews/report-integrity-final-audit-legacy.json").read_text(
            encoding="utf-8"
        )
    )
    assert legacy_validation["run_id"] == run_id
    assert legacy_validation["subject_ref"] == legacy_snapshot["canonical_markdown_ref"]
    assert legacy_validation["subject_revision"] == legacy_snapshot["subject_revision"]
    assert legacy_validation["passed"] is True
    handoff_contracts = json.loads(
        (run_root / "handoff-contracts.json").read_text(encoding="utf-8")
    )
    assert len(handoff_contracts) == 7
    assert handoff_contracts[0]["stage"] == "template-skill-read"
    template_provenance = json.loads(
        (run_root / "template-provenance.json").read_text(encoding="utf-8")
    )
    assert template_provenance["source"] == "packaged"
    assert template_provenance["selected_path"] == (
        "manyselves/templates/reporting/report_template.docx"
    )
    assert prepared["_declarative_delivery_context"]["output"] == (
        str(run_root / "report/配电安全专家咨询报告.docx")
    )
