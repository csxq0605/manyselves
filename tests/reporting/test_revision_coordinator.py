import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.agentic_models import (
    EditedReportSubmission,
    ModuleSubmission,
)
from manyselves.core.reporting.models import (
    EvidenceItem,
    ReportRequest,
    RevisionRequest,
    SourceLocation,
)
from manyselves.core.reporting.revisions import RevisionCoordinator
from manyselves.core.reporting.service import ReportingService
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.versions import ReportVersion
from manyselves.core.tools.task_board import TaskBoard


def test_revision_request_defaults_to_report_only_and_validates_scope() -> None:
    request = RevisionRequest(
        baseline_version_id="version-001",
        feedback="修订设备状态边界",
        target_module_ids=["2.4"],
        target_submodule_ids=["2.4.1.1"],
    )

    assert request.promote_to_skill is False
    with pytest.raises(ValidationError, match="promote_skill_id"):
        RevisionRequest(
            baseline_version_id="version-001",
            feedback="尝试演进",
            target_module_ids=["2.4"],
            promote_to_skill=True,
        )
    with pytest.raises(ValidationError, match="outside selected modules"):
        RevisionRequest(
            baseline_version_id="version-001",
            feedback="错误范围",
            target_module_ids=["2.4"],
            target_submodule_ids=["2.3.1"],
        )


@pytest.mark.asyncio
async def test_revision_of_unknown_baseline_fails_without_provider_call(tmp_path: Path) -> None:
    class NeverCalledProvider(LLMProvider):
        def __init__(self):
            super().__init__("test", model="never-called")

        async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
            raise AssertionError("missing baseline must fail before Agent execution")

    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )

    result = await service.revise(
        RevisionRequest(
            baseline_version_id="missing-version",
            feedback="修订设备状态边界",
            target_module_ids=["2.4"],
        )
    )

    assert result.status == "failed"
    assert "unknown report version" in (result.error or "")


def test_revision_restore_returns_current_run_chief_preparation_refs(
    tmp_path: Path,
) -> None:
    store = ReportingStore(tmp_path)
    modules = {
        module_id: ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                submodule_id: (
                    f"### {submodule_id}\n\n"
                    "基线正文说明现状、风险与验证边界。"
                )
                for submodule_id in definition.submodules
            },
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )
        for module_id, definition in REPORT_TAXONOMY.items()
    }
    edited = EditedReportSubmission(
        title="基线报告",
        assessment_background="基线评估背景。",
        findings_overview="基线发现概览。",
        regional_executive_summary="基线区域摘要。",
        module_narratives={
            module_id: submission.markdown
            for module_id, submission in modules.items()
        },
        risk_panorama="基线风险全景。",
        dimension_risk_analysis="基线维度风险分析。",
        data_gap_analysis="基线数据缺口分析。",
        improvement_action_plan="基线改进计划。",
    )
    evidence = EvidenceItem(
        id="E-0001",
        subject="基线检查项",
        fact="基线检查结果正常。",
        source=SourceLocation(
            file_id="F-0001",
            path=Path("Inputs/baseline.xlsx"),
            sheet="检查表",
            cell="A1",
        ),
        module_id="2.1",
        submodule_id="2.1.1",
    )
    artifact_refs: dict[str, Path] = {}
    for module_id, submission in modules.items():
        ref = Path(
            f"Work/baseline/modules/{module_id}.json"
        )
        store.write_json(
            ref.as_posix(),
            submission.model_dump(mode="json"),
        )
        artifact_refs[f"module_submission:{module_id}"] = ref
    for key, ref, payload in (
        (
            "edited_submission",
            Path("Work/baseline/edited.json"),
            edited.model_dump(mode="json"),
        ),
        (
            "report_request",
            Path("Work/baseline/request.json"),
            ReportRequest(
                instruction="生成基线报告。",
                missing_evidence_policy="draft",
            ).model_dump(mode="json"),
        ),
        (
            "source_ledger",
            Path("Work/baseline/sources.json"),
            [],
        ),
        (
            "photo_manifest",
            Path("Work/baseline/photo-manifest.json"),
            {"assets": []},
        ),
    ):
        store.write_json(ref.as_posix(), payload)
        artifact_refs[key] = ref
    evidence_ref = Path("Work/baseline/evidence.jsonl")
    store.write_jsonl(
        evidence_ref.as_posix(),
        [evidence.model_dump(mode="json")],
    )
    artifact_refs["evidence"] = evidence_ref
    baseline = ReportVersion(
        version_id="version-baseline",
        run_id="run-baseline",
        artifact_refs=artifact_refs,
        skill_provenance=[],
        session_summary_refs=[],
    )
    run_id = "report-revision-current-refs"
    coordinator = RevisionCoordinator(
        SimpleNamespace(
            workspace=tmp_path,
            store=store,
        ),
        None,
    )

    state, restored_edited = coordinator._restore(
        run_id,
        RevisionRequest(
            baseline_version_id=baseline.version_id,
            feedback="只修订模块 2.1。",
            target_module_ids=["2.1"],
        ),
        baseline,
    )

    current_evidence_ref = f"Work/runs/{run_id}/evidence.jsonl"
    current_photo_ref = (
        f"Work/runs/{run_id}/photo-manifest.json"
    )
    assert state["preparation_refs"] == {
        "evidence": current_evidence_ref,
        "photo_manifest": current_photo_ref,
    }
    assert state["evidence_items"] == [evidence]
    assert restored_edited == edited
    assert (
        tmp_path / current_evidence_ref
    ).read_text(encoding="utf-8") == (
        tmp_path / "Work/evidence.jsonl"
    ).read_text(encoding="utf-8")
    assert json.loads(
        (tmp_path / current_photo_ref).read_text(encoding="utf-8")
    ) == {"assets": []}
    assert json.loads(
        (tmp_path / "Work/photo-manifest.json").read_text(
            encoding="utf-8"
        )
    ) == {"assets": []}
