"""Characterization for the generic Capability-owned module revision boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossReviewFinding,
    ModuleReviewFinding,
    ModuleRevisionSubmission,
    ModuleSubmission,
    RevisionResponse,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    RequestedModuleChange,
    ValidationFailure,
    ValidationReport,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    UserSupplement,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    ModuleRevisionPreparation,
)
from manyselves.capabilities.distribution_reporting.runtime.module_revision_tools import (
    accept_module_revision,
    prepare_module_revision,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _module() -> ModuleSubmission:
    module_id = "2.1"
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"### {submodule_id}\n\n当前正文。"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )


@pytest.mark.asyncio
async def test_capability_revision_projection_keeps_mapping_request_supplement(
    tmp_path: Path,
) -> None:
    """Serialized request state still reaches the Provider-visible envelope."""

    run_id = "serialized-module-revision"
    store = ReportingStore(tmp_path)
    state = {
        "run_id": run_id,
        "request": {
            "user_supplements": [
                {
                    "id": "US-serialized",
                    "content": "序列化请求补充仍需进入作者任务。",
                    "scope": "module",
                    "target_ids": ["2.1"],
                    "stages": ["module_authoring"],
                }
            ]
        },
    }
    preparation = await prepare_module_revision(
        workspace=tmp_path,
        store=store,
        state=state,
        workflow_id="distribution-reporting",
        subject=_module(),
        requested_changes=[
            RequestedModuleChange(
                id="USER-2.1-R1",
                instruction="补充目标小节的责任、动作和验收闭环。",
                target_submodule_ids=["2.1.1"],
            )
        ],
        user_supplements=state["request"]["user_supplements"],
    )

    assert (
        "用户补充 US-serialized（scope=module; targets=2.1）是当前 run 的显式输入："
        "序列化请求补充仍需进入作者任务。"
        in preparation.envelope.constraints
    )


@pytest.mark.asyncio
async def test_generic_revision_preserves_all_trigger_contracts_and_barrier(
    tmp_path: Path,
) -> None:
    module = _module()
    run_id = "capability-module-revision"
    target_ids = ["2.1.1", "2.1.2", "2.1.3", "2.1.4"]
    module_finding = ModuleReviewFinding(
        id="M-2.1-initial-r0-1",
        target_submodule_id="2.1.1",
        category="evidence_boundary",
        impact="blocking",
        observation="当前目标小节缺少可以复核的证据边界和清晰的判断依据。",
        evidence_refs=[f"Work/runs/{run_id}/modules/2.1-r0.json"],
        required_change="补充证据边界、判断依据以及后续可以复核的执行闭环。",
        reviewer_checks=["确认目标小节已具备证据边界和复核依据。"],
    )
    cross_finding = CrossReviewFinding(
        id="X-2.1-r0-1",
        owner_module_id="2.1",
        target_submodule_ids=["2.1.2"],
        related_module_ids=["2.2"],
        category="dependencies",
        impact="blocking",
        observation="当前模块没有把该关系写入可验证的模块正文，导致关联模块无法复核其依赖边界。",
        evidence_refs=[f"Work/runs/{run_id}/modules/2.1-r0.json"],
        required_change="补充关系机制、影响路径和与关联模块的联合验证步骤，并明确可复核的证据边界。",
        reviewer_checks=["确认关系机制已写入目标小节并能由关联模块复核。"],
    )
    requested_change = RequestedModuleChange(
        id="USER-2.1-R1",
        instruction="补充目标小节的责任、动作和验收闭环。",
        target_submodule_ids=["2.1.3"],
    )
    validation_ref = f"Work/runs/{run_id}/reviews/module-quality-2.1-r0.json"
    validation_report = ValidationReport(
        validation_protocol_version=2,
        run_id=run_id,
        subject_ref=f"Work/runs/{run_id}/modules/2.1-r0.json",
        subject_revision=0,
        validator="module-structure/v2",
        check_ids=["module.canonical_markdown"],
        failures=[
            ValidationFailure(
                check_id="module.canonical_markdown",
                target_path="2.1.4",
                message="目标小节需要一次显式结构修订。",
            )
        ],
        passed=False,
    )
    store = ReportingStore(tmp_path)
    store.write_json(validation_ref, validation_report.model_dump(mode="json"))
    validation_calls: list[tuple[str, int]] = []

    def validate_binding(report: ValidationReport, subject_ref: str, revision: int) -> None:
        validation_calls.append((subject_ref, revision))
        assert report == validation_report

    preparation = await prepare_module_revision(
        workspace=tmp_path,
        store=store,
        state={"run_id": run_id, "request": {"instruction": "保留独立验收样例及其未复核边界。"}},
        workflow_id="distribution-reporting",
        subject=module,
        module_findings=[module_finding],
        cross_findings=[cross_finding],
        requested_changes=[requested_change],
        validation_ref=validation_ref,
        validation_target_submodule_ids={"2.1.4"},
        validate_validation_binding=validate_binding,
        user_supplements=[
            UserSupplement(
                id="US-module-revision",
                content="用户补充约束保持在原任务边界内。",
                scope="module",
                target_ids=["2.1"],
                stages=["module_authoring"],
            )
        ],
    )

    assert isinstance(preparation, ModuleRevisionPreparation)
    assert preparation.target_submodule_ids == target_ids
    assert preparation.required_finding_ids == sorted(
        [module_finding.id, cross_finding.id, requested_change.id]
    )
    assert preparation.revision_input.module_findings == [module_finding]
    assert preparation.revision_input.report_instruction == "保留独立验收样例及其未复核边界。"
    assert preparation.revision_input.cross_findings == [cross_finding]
    assert preparation.revision_input.requested_changes == [requested_change]
    assert preparation.revision_input.validation_report == validation_report
    assert (
        "用户补充 US-module-revision（scope=module; targets=2.1）是当前 run 的显式输入："
        "用户补充约束保持在原任务边界内。"
        in preparation.envelope.constraints
    )
    assert validation_calls == [(f"Work/runs/{run_id}/modules/2.1-r0.json", 0)]

    revised, subject_ref = accept_module_revision(
        workspace=tmp_path,
        store=store,
        preparation=preparation,
        result=ModuleRevisionSubmission(
            module_id="2.1",
            base_revision=0,
            revision=1,
            submodule_narratives={
                target_id: f"### {target_id}\n\n已按当前任务边界完成修订。"
                for target_id in target_ids[:3]
            },
            source_ids=[],
            unresolved_questions=[],
            revision_responses=[
                RevisionResponse(
                    finding_id=finding_id,
                    action="implemented",
                    summary="已完成对应目标的内容修订并保留后续复核依据。",
                    changed_target_ids=[target_id],
                )
                for finding_id, target_id in (
                    (module_finding.id, "2.1.1"),
                    (cross_finding.id, "2.1.2"),
                    (requested_change.id, "2.1.3"),
                )
            ],
        ),
    )
    assert revised.revision == 1
    assert subject_ref == f"Work/runs/{run_id}/modules/2.1-r1.json"
    barrier = json.loads(
        (
            tmp_path / f"Work/runs/{run_id}/reviews/module-revisions/2.1/r1/module-barrier.json"
        ).read_text(encoding="utf-8")
    )
    assert barrier["subject_ref"] == subject_ref
    assert barrier["target_submodule_ids"] == target_ids
    assert (
        barrier["subject_sha256"]
        == hashlib.sha256((tmp_path / subject_ref).read_bytes()).hexdigest()
    )
