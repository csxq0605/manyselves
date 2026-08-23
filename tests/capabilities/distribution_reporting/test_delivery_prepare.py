from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
    compose_module_markdown,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ClaimRecord,
    EditedReportSubmission,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    EvidenceItem,
    ReportRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.core.reporting.declarative_delivery import DeclarativeDeliveryRuntime


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

    class _Runner:
        service = SimpleNamespace(
            workspace=tmp_path,
            store=store,
            resolve_report_template=lambda _run_id: (
                Path("manyselves/templates/reporting/report_template.docx").resolve(),
                "packaged",
            ),
        )

        def _prepare_and_render_delivery(self, _state: dict) -> None:
            raise AssertionError("production prepare must not call Runner private prepare")

        def _validated_final_audit_subject(
            self,
            _state: dict,
        ) -> tuple[EditedReportSubmission, str]:
            assert isinstance(_state["request"], ReportRequest)
            assert isinstance(_state["edited_report"], EditedReportSubmission)
            assert all(
                isinstance(value, ModuleSubmission)
                for value in _state["module_submissions"].values()
            )
            assert all(isinstance(value, EvidenceItem) for value in _state["evidence_items"])
            return edited, f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"

        def _write_handoff_contracts(self, state: dict) -> Path:
            return store.write_json(
                f"Work/runs/{state['run_id']}/handoff-contracts.json",
                {"run_id": state["run_id"]},
            )

        def _delivery_projection(
            self,
            _state: dict,
            subject: EditedReportSubmission,
            _claims: list,
        ) -> None:
            raise AssertionError(
                "production prepare must not call Runner private delivery projection"
            )

        def _validate_final_report_structure(
            self,
            _state: dict,
            _markdown: str,
            _phase: str,
        ) -> None:
            raise AssertionError(
                "production prepare must not call Runner private report validation"
            )

    state = {
        "run_id": run_id,
        "request": ReportRequest(instruction="Render the accepted report."),
        "module_submissions": _module_submissions(),
        "evidence_items": [],
        "photo_assets": [],
        "edited_report": edited,
    }
    serialized = json.loads(
        json.dumps(
            {
                "run_id": state["run_id"],
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
    prepared = DeclarativeDeliveryRuntime(_Runner()).prepare(serialized)

    run_root = tmp_path / "Work" / "runs" / run_id
    expected_files = (
        "ledgers/claims.json",
        "ledgers/sources.json",
        "evidence.jsonl",
        "edited-submission.json",
        "request-snapshot.json",
        "photo-manifest.json",
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
    assert prepared["_declarative_delivery_context"]["output"] == (
        str(run_root / "report/配电安全专家咨询报告.docx")
    )
