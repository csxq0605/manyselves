"""Characterization for the Capability-owned Cross owner revision boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossOwnerFindingSubmission,
    CrossReviewCoverageEntry,
    CrossReviewFinding,
    ModuleRevisionSubmission,
    ModuleSubmission,
    RevisionResponse,
)
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerInitialAgentResult,
    DeclarativeCrossOwnerRuntimeContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRevisionAgentResult,
)


def _module(module_id: str) -> ModuleSubmission:
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"内容 {submodule_id}"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )


def _state(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "request": {
            "user_supplements": [
                {
                    "id": "US-cross-revision",
                    "content": "Cross 修订必须保留该用户约束。",
                    "scope": "submodule",
                    "target_ids": ["2.1.1"],
                    "stages": ["module_authoring"],
                }
            ]
        },
        "module_submissions": {
            module_id: _module(module_id) for module_id in REPORT_TAXONOMY
        },
        "module_artifact_refs": {
            module_id: {
                "ref": f"Work/runs/{run_id}/modules/{module_id}-r0.json",
                "sha256": "0" * 64,
            }
            for module_id in REPORT_TAXONOMY
        },
    }


def _finding() -> CrossReviewFinding:
    return CrossReviewFinding(
        id="X-2.1-r0-1",
        owner_module_id="2.1",
        target_submodule_ids=["2.1.1"],
        related_module_ids=["2.2"],
        category="dependencies",
        impact="blocking",
        observation="当前模块的关联关系没有在目标正文中形成可复核的机制、影响路径与联合验证闭环。",
        evidence_refs=["Work/runs/cross-revision/modules/2.1-r0.json"],
        required_change="在目标小节补充完整关系机制、影响传播路径、责任边界与可复核的联合验证步骤。",
        reviewer_checks=["确认目标小节包含关系机制与联合验证。"],
    )


@pytest.mark.asyncio
async def test_cross_revision_preserves_typed_supplement_barrier_and_reuse(
    tmp_path: Path,
) -> None:
    run_id = "cross-revision"
    runtime = CrossOwnerRuntime(tmp_path)
    state = _state(run_id)
    owner = _module("2.1")
    owner_ref = f"Work/runs/{run_id}/modules/2.1-r0.json"
    runtime.store.write_json(owner_ref, owner.model_dump(mode="json"))
    completion_ref = f"Work/runs/{run_id}/reviews/module/initial/2.1/completion-r0.json"
    runtime.store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.1",
            subject_refs=[owner_ref],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )
    state["module_review_completion_refs"] = {"2.1": completion_ref}

    prepared_state = runtime.prepare(state)
    initial = runtime.prepare_initial(
        {"state": prepared_state, "owner_module_id": "2.1"}
    )
    assert initial.preparation is not None
    assert [item.id for item in initial.preparation.user_supplements] == [
        "US-cross-revision"
    ]
    initial = runtime.accept_initial(
        {
            "context": initial,
            "result": DeclarativeCrossOwnerInitialAgentResult(
                status="completed",
                submission=CrossOwnerFindingSubmission(
                    owner_module_id="2.1",
                    coverage=CrossReviewCoverageEntry(
                        module_id="2.1",
                        checked_dimensions=[
                            "terminology",
                            "facts",
                            "risk_levels",
                            "dependencies",
                            "propagation",
                            "joint_verification",
                        ],
                    ),
                    findings=[_finding()],
                ),
            ),
        }
    )
    assert initial.acceptance is not None
    assert [item.id for item in initial.acceptance.user_supplements] == [
        "US-cross-revision"
    ]

    revision = await runtime.prepare_revision(initial)
    assert revision.status == "revision_ready"
    assert runtime.revision_requires_agent(revision)
    assert revision.revision_preparation is not None
    assert revision.revision_preparation.review_round == 1
    assert revision.revision_preparation.prepared is not None
    assert (
        "用户补充 US-cross-revision（scope=submodule; targets=2.1.1）是当前 run 的显式输入："
        "Cross 修订必须保留该用户约束。"
        in revision.revision_preparation.prepared.envelope.constraints
    )
    restored = DeclarativeCrossOwnerRuntimeContext.model_validate(
        initial.model_dump(mode="json")
    )
    assert [item.id for item in restored.acceptance.user_supplements] == [
        "US-cross-revision"
    ]

    revised = ModuleRevisionSubmission(
        module_id="2.1",
        base_revision=0,
        revision=1,
        submodule_narratives={"2.1.1": "补充关系机制与联合验证。"},
        source_ids=[],
        revision_responses=[
            RevisionResponse(
                finding_id="X-2.1-r0-1",
                action="implemented",
                summary="已在目标小节完整补充关系机制、影响传播路径与可复核的联合验证步骤。",
                changed_target_ids=["2.1.1"],
            )
        ],
    )
    agent_result = DeclarativeModuleRevisionAgentResult(
        status="completed",
        submission=revised,
    )
    accepted = runtime.accept_revision(
        {
            "context": revision,
            "result": agent_result,
        }
    )
    assert accepted.status == "revision_accepted"
    assert accepted.revision_acceptance is not None
    assert [item.id for item in accepted.revision_acceptance.user_supplements] == [
        "US-cross-revision"
    ]
    subject_ref = f"Work/runs/{run_id}/modules/2.1-r1.json"
    barrier_ref = (
        f"Work/runs/{run_id}/reviews/module-revisions/2.1/r1/module-barrier.json"
    )
    subject_path = tmp_path / subject_ref
    barrier = json.loads((tmp_path / barrier_ref).read_text(encoding="utf-8"))
    assert barrier["subject_ref"] == subject_ref
    assert barrier["subject_sha256"] == hashlib.sha256(subject_path.read_bytes()).hexdigest()

    reused = await runtime.prepare_revision(restored)
    assert reused.status == "revision_resumed"
    assert not runtime.revision_requires_agent(reused)
    assert reused.revision_preparation is not None
    assert reused.revision_preparation.mode == "continue_existing"
    assert reused.revision_preparation.existing_candidate_ref == subject_ref
    assert reused.revision_acceptance is not None
    assert reused.revision_acceptance.revised.revision == 1
    # Existing same-run results win without adding a rejection gate or rewrite.
    resumed = runtime.accept_revision({"context": reused, "result": agent_result})
    assert resumed.status == "revision_accepted"
    assert resumed.revision_acceptance is not None
    assert resumed.revision_acceptance.candidate_ref == subject_ref
