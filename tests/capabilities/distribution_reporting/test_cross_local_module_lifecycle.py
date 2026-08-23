"""Characterization for the Cross local Module lifecycle boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_definitions import (
    register_cross_owner_pipeline_specializations,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleReviewCoverage,
    ModuleReviewFinding,
    ModuleReviewFindingSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerRuntimeContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    CrossOwnerRevisionAcceptance,
)
from manyselves.kernel.definitions import DefinitionKind
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import SubworkflowAction, WorkflowCompiler
from tests.capabilities.distribution_reporting.test_cross_owner_local_review import (
    _finding,
    _module,
    _write_prior_completion,
)


def _revision_context(
    tmp_path: Path,
    *,
    run_id: str,
    revised=None,
) -> tuple[CrossOwnerRuntime, DeclarativeCrossOwnerRuntimeContext, str]:
    runtime = CrossOwnerRuntime(tmp_path)
    baseline = _module(revision=0)
    revised = _module(revision=1, changed=True) if revised is None else revised
    baseline_ref = f"Work/runs/{run_id}/modules/2.1-r0.json"
    completion_ref = (
        f"Work/runs/{run_id}/reviews/module/initial/2.1/completion-r0.json"
    )
    runtime.store.write_json(baseline_ref, baseline.model_dump(mode="json"))
    _write_prior_completion(
        runtime.store,
        run_id=run_id,
        subject_ref=baseline_ref,
        completion_ref=completion_ref,
    )
    finding = _finding().model_copy(update={"evidence_refs": [baseline_ref]})
    context = DeclarativeCrossOwnerRuntimeContext(
        owner_module_id="2.1",
        status="revision_accepted",
        revision_acceptance=CrossOwnerRevisionAcceptance(
            run_id=run_id,
            workflow_id="distribution-cross-owner-2.1-pipeline",
            owner_module_id="2.1",
            review_round=1,
            owner_input_ref=(
                f"Work/runs/{run_id}/reviews/cross-owner-input-r0-2.1.json"
            ),
            current=baseline,
            findings=[finding],
            finding_refs=[
                f"Work/runs/{run_id}/reviews/cross-owner-findings-r0-2.1.json"
            ],
            prior_completion_ref=completion_ref,
            revised=revised,
            candidate_ref=f"Work/runs/{run_id}/modules/2.1-r1.json",
        ),
    )
    return runtime, context, baseline_ref


@pytest.mark.asyncio
async def test_cross_local_preflight_revision_is_projected_into_module_lane(
    tmp_path: Path,
) -> None:
    """A local machine-preflight result remains in the Capability lane state."""

    revised = _module(revision=1, changed=True)
    invalid_claim = revised.claims[0].model_copy(update={"source_ids": ["E-missing"]})
    revised = revised.model_copy(
        update={"claims": [invalid_claim], "source_ids": ["E-missing"]}
    )
    runtime, context, _ = _revision_context(
        tmp_path,
        run_id="cross-local-preflight-boundary",
        revised=revised,
    )

    prepared = await runtime.prepare_local_review(context)

    assert prepared.local_review_preparation is not None
    assert prepared.local_review_preparation.mode == "preflight_revision"
    assert prepared.local_module_context is not None
    assert prepared.local_module_context.status == "preflight_revision_pending"


def test_cross_pipeline_requires_local_module_subworkflow_before_recheck() -> None:
    """The file-defined Cross pipeline currently jumps directly to Cross recheck."""

    _, definitions = load_distribution_reporting_capability()
    register_cross_owner_pipeline_specializations(definitions)
    workflow = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        definitions,
    )

    local_prepare_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.id == "prepare-current-cross-owner-local-review"
    )
    recheck_index = next(
        index
        for index, action in enumerate(plan.actions)
        if action.id == "prepare-current-cross-owner-recheck"
    )
    local_module_subworkflow = next(
        (
            (index, action)
            for index, action in enumerate(plan.actions)
            if isinstance(action, SubworkflowAction)
            and action.workflow
            == "distribution-cross-owner-local-2.1-module-review-lane"
        ),
        None,
    )

    assert local_module_subworkflow is not None
    subworkflow_index, _ = local_module_subworkflow
    assert local_prepare_index < subworkflow_index < recheck_index

    child = plan.subworkflow_plans[
        "distribution-cross-owner-local-2.1-module-review-lane"
    ]
    assert any(
        action.id == "accept-current-cross-owner-local-review"
        for action in child.actions
    )


@pytest.mark.asyncio
async def test_cross_local_finding_requires_module_revision_before_cross_recheck(
    tmp_path: Path,
) -> None:
    """A local Auditor finding must not be sent straight to Cross recheck."""

    runtime, context, baseline_ref = _revision_context(
        tmp_path,
        run_id="cross-local-finding-boundary",
    )
    prepared = await runtime.prepare_local_review(context)
    local_finding = ModuleReviewFinding(
        id="M-2.1-cross-r1-r0-1",
        target_submodule_id="2.1.1",
        category="analysis_depth",
        impact="blocking",
        observation="本地回归发现修订内容没有完整说明联合验证边界。",
        evidence_refs=[baseline_ref],
        required_change="补充联合验证步骤和回归影响说明，供原审查者复核。",
        reviewer_checks=["确认联合验证步骤和回归影响已写入目标小节。"],
    )
    accepted = runtime.accept_local_review(
        {
            "context": prepared,
            "result": ModuleReviewFindingSubmission(
                coverage=ModuleReviewCoverage(submodule_ids=["2.1.1"]),
                findings=[local_finding],
            ),
        }
    )
    assert accepted.local_review_acceptance is not None
    assert accepted.local_review_acceptance.review.next_action == "revise"

    revision_ready = await runtime.prepare_local_module_revision(accepted)
    assert revision_ready.local_module_context is not None
    assert revision_ready.local_module_context.status == "revision_ready"
