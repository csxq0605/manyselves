from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from manyselves.core.reporting import review_lifecycle as lifecycle
from manyselves.core.reporting.agentic_models import (
    CROSS_REVIEW_DIMENSIONS,
    CrossOwnerFindingSubmission,
    CrossOwnerVerdictSubmission,
    CrossReviewCoverageEntry,
    CrossReviewFinding,
    CrossSynthesisInput,
    ModuleReviewFindingSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
)
from manyselves.core.reporting.declarative_cross_owner_cohort import (
    DeclarativeCrossOwnerInitialAgentResult,
    DeclarativeCrossOwnerPipelineOutcome,
    DeclarativeCrossOwnerRuntime,
    compile_cross_owner_workflows,
    retry_failed_cross_owner_pipelines,
)
from manyselves.core.reporting.declarative_reporting_tail import (
    build_reporting_tail_definition,
)
from manyselves.core.reporting.input_contracts import (
    CrossOwnerInput,
    ReviewCompletionRecord,
    ValidationReport,
)
from manyselves.core.reporting.parallel_runtime import (
    ArtifactRef,
    CrossOwnerCompletion,
    TaskCorrelation,
    TaskTerminal,
)
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.workflow import ReportWorkflowRunner
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationMode,
    ConversationRegistry,
)
from manyselves.kernel.definitions import DefinitionKind
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.workflow import WorkflowState, WorkflowStatus
from manyselves.runtime.state_store import FileWorkflowStateStore, InMemoryWorkflowStateStore
from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink, WorkflowRuntimeHost


def _module(module_id: str, revision: int = 0) -> ModuleSubmission:
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"### {submodule_id}\n\nmodule body"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=revision,
    )


def _synthesis(owner_module_id: str) -> list[CrossSynthesisInput]:
    shared = {
        "related_module_ids": ["2.1", "2.2"],
        "root_causes": ["2.1 与 2.2 共同根因"],
        "propagation_steps": [
            "2.1 边界条件影响 2.2 的运行裕度",
            "2.2 运行变化反过来扩大 2.1 的处置影响",
        ],
        "causal_chain": "2.1 与 2.2 的边界条件叠加后会共同扩大故障影响范围。",
        "decision_implication": "管理层需要按共同根因安排联合整改并保留剩余边界。",
        "action_dependencies": ["先确认 2.1 边界，再处理 2.2 条件"],
        "joint_actions": ["由 2.1 与 2.2 责任方共同完成联合复测"],
        "verification_method": "通过联合试验、记录核对和复测结果完成验收。",
        "acceptance_criteria": ["2.1 与 2.2 的复测记录均达到约定关闭条件"],
        "module_statement_refs": ["2.1.1", "2.2.1"],
        "confidence_and_boundary": "当前证据边界明确，未验证条件必须保留并标注。",
        "target_report_section_ids": ["3.1.1"],
        "evidence_refs": ["E-0001"],
    }
    return [
        CrossSynthesisInput(
            id=f"SI-{owner_module_id}-RISK",
            cluster_type="risk_cluster",
            **shared,
        ),
        CrossSynthesisInput(
            id=f"SI-{owner_module_id}-GLOBAL",
            cluster_type="global_propagation",
            **shared,
        ),
    ]


def _cross_finding(finding_id: str) -> CrossReviewFinding:
    return CrossReviewFinding(
        id=finding_id,
        owner_module_id="2.1",
        target_submodule_ids=["2.1.1"],
        related_module_ids=["2.2"],
        category="dependencies",
        impact="blocking",
        observation=(
            "模块2.1当前写法没有稳定表达与模块2.2之间的实施先后关系，"
            "可能导致责任边界和联合验收顺序发生冲突。"
        ),
        evidence_refs=["Work/runs/run-cross-regression/modules/2.1-r0.json"],
        required_change=(
            "在模块2.1责任范围内补充与模块2.2的实施顺序、接口责任人和联合验收条件，"
            "并保留可由同一Cross owner复核的明确记录。"
        ),
        reviewer_checks=["实施顺序、接口责任和联合验收条件均已明确写入。"],
    )


class _Runner:
    def __init__(self, workspace: Path, *, failed_owner: str | None = None) -> None:
        self.service = SimpleNamespace(
            workspace=workspace,
            store=ReportingStore(workspace),
        )
        self.failed_owner = failed_owner
        self.calls: list[tuple[str, str]] = []

    async def _agent(
        self,
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        owner_module_id = envelope.task_id.split("-")[2]
        self.calls.append((owner_module_id, session_key or ""))
        await asyncio.sleep(0)
        if owner_module_id == self.failed_owner:
            raise RuntimeError(f"injected owner failure: {owner_module_id}")
        return CrossOwnerFindingSubmission(
            owner_module_id=owner_module_id,
            coverage=CrossReviewCoverageEntry(
                module_id=owner_module_id,
                checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
            ),
            findings=[],
            synthesis_inputs=_synthesis(owner_module_id),
        )

    @staticmethod
    def _role_skill_context(_state, _role, **_kwargs):
        return ""

    @staticmethod
    def _user_supplement_constraints(_state, **_kwargs):
        return []

    def _load_current_review_completion(self, **kwargs):
        return ReportWorkflowRunner._load_current_review_completion(self, **kwargs)


class _CrossFindingRunner(_Runner):
    """Provider double for the next Cross finding-to-author characterization."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.agent_calls: list[tuple[str, str, str]] = []
        self.local_review_inputs: list[lifecycle.ModuleReviewInput] = []

    async def _agent(
        self,
        agent_id,
        envelope,
        artifacts,
        workflow_id,
        *,
        session_key=None,
    ):
        del artifacts
        session = session_key or ""
        self.agent_calls.append((agent_id, envelope.task_id, session))
        if agent_id == "cross-module-reviewer":
            owner_module_id = envelope.task_id.split("-")[2]
            self.calls.append((owner_module_id, session))
            if not envelope.task_id.endswith("initial"):
                contract = CrossOwnerInput.model_validate_json(
                    (self.service.workspace / envelope.input_refs[0]).read_text(
                        encoding="utf-8"
                    )
                )
                return CrossOwnerVerdictSubmission(
                    owner_module_id=owner_module_id,
                    coverage=CrossReviewCoverageEntry(
                        module_id=owner_module_id,
                        checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                    ),
                    verdicts=[
                        ResolutionVerdict(
                            finding_id=finding.id,
                            verdict="resolved",
                            reason="当前模块修订已经满足 Cross owner 的全部检查项。",
                            evidence_refs=[contract.owner_subject_ref],
                        )
                        for finding in contract.required_findings
                    ],
                    new_findings=[],
                )
            return CrossOwnerFindingSubmission(
                owner_module_id=owner_module_id,
                coverage=CrossReviewCoverageEntry(
                    module_id=owner_module_id,
                    checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                ),
                findings=(
                    [_cross_finding("XMR-2.1-001")]
                    if envelope.task_id.endswith("initial")
                    else []
                ),
                synthesis_inputs=_synthesis(owner_module_id),
            )
        if agent_id == "module-2.1-specialist":
            self.calls.append(("2.1", session))
            target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
            return ModuleRevisionSubmission(
                module_id="2.1",
                base_revision=0,
                revision=1,
                submodule_narratives={target: f"{target} revised for Cross owner finding"},
                source_ids=[],
                unresolved_questions=[],
                revision_responses=[
                    RevisionResponse(
                        finding_id="XMR-2.1-001",
                        action="implemented",
                        summary=(
                            "补充 Cross owner finding 要求的模块接口与联合验证条件。"
                        ),
                        changed_target_ids=[target],
                    )
                ],
            )
        if agent_id == "evidence-auditor":
            review_input = lifecycle.ModuleReviewInput.model_validate_json(
                (self.service.workspace / envelope.input_refs[0]).read_text(
                    encoding="utf-8"
                )
            )
            self.local_review_inputs.append(review_input)
            self.calls.append((review_input.module_id, session))
            return ModuleReviewFindingSubmission(
                coverage={"submodule_ids": review_input.required_submodule_ids},
                findings=[],
            )
        raise AssertionError(f"unexpected Agent invocation: {agent_id}/{envelope.task_id}")

    def _validate_module_structure(self, state, module, phase):
        subject_ref = (
            f"Work/runs/{state['run_id']}/modules/"
            f"{module.module_id}-r{module.revision}.json"
        )
        ref = (
            f"Work/runs/{state['run_id']}/reviews/"
            f"module-quality-{module.module_id}-r{module.revision}-{phase}.json"
        )
        self.service.store.write_json(
            ref,
            ValidationReport(
                validation_protocol_version=2,
                run_id=state["run_id"],
                subject_ref=subject_ref,
                subject_revision=module.revision,
                validator="test-module-structure/v2",
                check_ids=["module.canonical_markdown"],
                passed=True,
            ).model_dump(mode="json"),
        )
        return ref

    @staticmethod
    def _template_skill_context(_state, *_parts):
        return ""


def _artifact_ref(runner: _Runner, ref: str) -> ArtifactRef:
    content = (runner.service.workspace / ref).read_bytes()
    return ArtifactRef(
        ref=ref,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        media_type="application/json",
    )


def _fake_noop(
    runner: _Runner,
    *,
    state: dict,
    owner_module_id: str,
    module: ModuleSubmission,
    owner_input_ref: str,
    review_round: int,
) -> lifecycle._CrossOwnerLaneResult:
    run_id = state["run_id"]
    subject_ref = f"Work/runs/{run_id}/modules/{owner_module_id}-r{module.revision}.json"
    local_ref = (
        f"Work/runs/{run_id}/reviews/module/cross-r{review_round}/"
        f"{owner_module_id}/completion.json"
    )
    runner.service.store.write_json(
        local_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key=f"module-auditor-{owner_module_id}",
            subject_refs=[subject_ref],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )
    completion = CrossOwnerCompletion(
        lane_id=f"cross-r{review_round}-module-{owner_module_id}",
        run_id=run_id,
        review_round=review_round,
        module_id=owner_module_id,
        semantic_key=hashlib.sha256(owner_module_id.encode()).hexdigest(),
        subject_revision=module.revision,
        owner_input=_artifact_ref(runner, owner_input_ref),
        subject=_artifact_ref(runner, subject_ref),
        local_review_completion=_artifact_ref(runner, local_ref),
        author_task_attempt_id=f"noop-{owner_module_id}",
        reviewer_session_id=f"cross-owner-{owner_module_id}",
        lease_epoch=1,
    )
    completion_ref = (
        f"Work/runs/{run_id}/lanes/cross-r{review_round}/"
        f"module-{owner_module_id}/completion-r{module.revision}.json"
    )
    runner.service.store.write_json(completion_ref, completion.model_dump(mode="json"))
    return lifecycle._CrossOwnerLaneResult(
        module=module,
        responses=[],
        local_review_ref=local_ref,
        completion_ref=completion_ref,
        completion=completion,
    )


def test_cross_owner_completion_rejects_local_review_for_other_subject(
    tmp_path: Path,
) -> None:
    run_id = "run-cross-local-binding"
    runner = _Runner(tmp_path)
    _write_modules(runner, run_id)
    state = _state(run_id)
    _owner_input, owner_input_ref = lifecycle._cross_owner_input(
        runner,
        state=state,
        modules=state["module_submissions"],
        owner_module_id="2.1",
        phase="initial",
        review_round=0,
    )
    result = _fake_noop(
        runner,
        state=state,
        owner_module_id="2.1",
        module=state["module_submissions"]["2.1"],
        owner_input_ref=owner_input_ref,
        review_round=0,
    )
    local_ref = result.local_review_ref
    runner.service.store.write_json(
        local_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.1",
            subject_refs=[f"Work/runs/{run_id}/modules/2.2-r0.json"],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )

    with pytest.raises(lifecycle.ReviewLifecycleError, match="does not bind subject"):
        lifecycle._validate_cross_owner_completion_business_identity(
            runner,
            result.completion,
            run_id=run_id,
            owner_module_id="2.1",
            review_round=0,
            owner_input_ref=owner_input_ref,
        )


async def _fake_revision_lane(
    runner: _Runner,
    *,
    state: dict,
    module_id: str,
    current: ModuleSubmission,
    findings: list[CrossReviewFinding],
    review_round: int,
    owner_input_ref: str,
    **_kwargs,
) -> lifecycle._CrossOwnerLaneResult:
    run_id = state["run_id"]
    responses = [
        RevisionResponse(
            finding_id=finding.id,
            action="implemented",
            summary="责任模块已按本轮Cross finding补充接口责任、实施顺序和联合验收条件。",
            changed_target_ids=list(finding.target_submodule_ids),
        )
        for finding in findings
    ]
    revised = current.model_copy(
        update={
            "revision": current.revision + 1,
            "revision_responses": responses,
        }
    )
    subject_ref = (
        f"Work/runs/{run_id}/modules/{module_id}-r{revised.revision}.json"
    )
    runner.service.store.write_json(
        subject_ref, revised.model_dump(mode="json")
    )
    local_ref = (
        f"Work/runs/{run_id}/reviews/module/cross-r{review_round}/"
        f"{module_id}/completion.json"
    )
    runner.service.store.write_json(
        local_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key=f"module-auditor-{module_id}",
            subject_refs=[subject_ref],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )
    completion = CrossOwnerCompletion(
        lane_id=f"cross-r{review_round}-module-{module_id}",
        run_id=run_id,
        review_round=review_round,
        module_id=module_id,
        semantic_key=hashlib.sha256(
            f"{module_id}:{review_round}".encode()
        ).hexdigest(),
        subject_revision=revised.revision,
        owner_input=_artifact_ref(runner, owner_input_ref),
        subject=_artifact_ref(runner, subject_ref),
        local_review_completion=_artifact_ref(runner, local_ref),
        author_task_attempt_id=f"test-author-{module_id}-r{review_round}",
        reviewer_session_id=f"cross-owner-{module_id}",
        lease_epoch=1,
    )
    completion_ref = (
        f"Work/runs/{run_id}/lanes/cross-r{review_round}/"
        f"module-{module_id}/completion-r{revised.revision}.json"
    )
    runner.service.store.write_json(
        completion_ref, completion.model_dump(mode="json")
    )
    return lifecycle._CrossOwnerLaneResult(
        module=revised,
        responses=responses,
        local_review_ref=local_ref,
        completion_ref=completion_ref,
        completion=completion,
    )


def _state(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "module_submissions": {
            module_id: _module(module_id) for module_id in REPORT_TAXONOMY
        },
        "module_review_completion_refs": {},
    }


def _write_modules(runner: _Runner, run_id: str) -> None:
    for module_id in REPORT_TAXONOMY:
        runner.service.store.write_json(
            f"Work/runs/{run_id}/modules/{module_id}-r0.json",
            _module(module_id).model_dump(mode="json"),
        )


def _write_initial_module_completion(
    runner: _Runner,
    state: dict,
    module_id: str,
) -> str:
    run_id = state["run_id"]
    ref = f"Work/runs/{run_id}/reviews/module/initial/{module_id}/completion.json"
    runner.service.store.write_json(
        ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key=f"module-auditor-{module_id}",
            subject_refs=[f"Work/runs/{run_id}/modules/{module_id}-r0.json"],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )
    state["module_review_completion_refs"][module_id] = ref
    return ref


async def _execute_declarative_cross_owner_cohort(
    runner: _Runner,
    state: dict,
    workflow_id: str,
) -> tuple[dict, WorkflowState]:
    definitions, contracts, _tail = build_reporting_tail_definition()
    executors = build_builtin_executor_registry()
    cohort_plan, pipeline_plans = compile_cross_owner_workflows(
        definitions,
        executors,
    )
    runtime = DeclarativeCrossOwnerRuntime(runner, state, workflow_id)
    kernel_state = WorkflowState.for_plan(state["run_id"], cohort_plan)
    kernel_state.variables["reporting-state"] = state
    completed = await WorkflowRuntimeHost(
        executors,
        InMemoryWorkflowStateStore(),
        InMemoryWorkflowEventSink(),
    ).execute(
        cohort_plan,
        kernel_state,
        RuntimeContext(
            tools={
                "prepare-cross-owner-cohort": runtime.prepare,
                "prepare-current-cross-owner-initial": runtime.prepare_initial,
                "cross-owner-initial-requires-agent": runtime.initial_requires_agent,
                "accept-current-cross-owner-initial": runtime.accept_initial,
                "cross-owner-initial-has-findings": runtime.initial_has_findings,
                "prepare-current-cross-owner-revision": runtime.prepare_revision,
                "cross-owner-revision-requires-agent": (
                    runtime.revision_requires_agent
                ),
                "accept-current-cross-owner-revision": runtime.accept_revision,
                "prepare-current-cross-owner-local-review": (
                    runtime.prepare_local_review
                ),
                "cross-owner-local-review-requires-agent": (
                    runtime.local_review_requires_agent
                ),
                "accept-current-cross-owner-local-review": (
                    runtime.accept_local_review
                ),
                "continue-current-cross-owner-pipeline": runtime.continue_owner,
                "reduce-cross-owner-cohort": runtime.reduce,
            },
            agents=runtime.agent_invokers,
            contracts=contracts,
            definitions=definitions,
            subworkflows=pipeline_plans,
        ),
    )
    assert completed.status is WorkflowStatus.COMPLETED
    return completed.outputs["result"], completed


async def _execute_declarative_cross_owner_pipeline(
    runner: _Runner,
    state: dict,
    workflow_id: str,
    *,
    owner_module_id: str = "2.1",
) -> WorkflowState:
    definitions, contracts, _tail = build_reporting_tail_definition()
    executors = build_builtin_executor_registry()
    _cohort_plan, pipeline_plans = compile_cross_owner_workflows(
        definitions,
        executors,
    )
    pipeline_plan = pipeline_plans[
        f"distribution-cross-owner-{owner_module_id}-pipeline"
    ]
    runtime = DeclarativeCrossOwnerRuntime(runner, state, workflow_id)
    kernel_state = WorkflowState.for_plan(state["run_id"], pipeline_plan)
    kernel_state.variables["reporting-state"] = state
    return await WorkflowRuntimeHost(
        executors,
        InMemoryWorkflowStateStore(),
        InMemoryWorkflowEventSink(),
    ).execute(
        pipeline_plan,
        kernel_state,
        RuntimeContext(
            tools={
                "prepare-current-cross-owner-initial": runtime.prepare_initial,
                "cross-owner-initial-requires-agent": runtime.initial_requires_agent,
                "accept-current-cross-owner-initial": runtime.accept_initial,
                "cross-owner-initial-has-findings": runtime.initial_has_findings,
                "prepare-current-cross-owner-revision": runtime.prepare_revision,
                "cross-owner-revision-requires-agent": (
                    runtime.revision_requires_agent
                ),
                "accept-current-cross-owner-revision": runtime.accept_revision,
                "prepare-current-cross-owner-local-review": (
                    runtime.prepare_local_review
                ),
                "cross-owner-local-review-requires-agent": (
                    runtime.local_review_requires_agent
                ),
                "accept-current-cross-owner-local-review": (
                    runtime.accept_local_review
                ),
                "continue-current-cross-owner-pipeline": runtime.continue_owner,
            },
            agents=runtime.agent_invokers,
            contracts=contracts,
            definitions=definitions,
            subworkflows=pipeline_plans,
        ),
    )


def _write_legacy_completed_task_binding(
    runner: _Runner,
    *,
    run_id: str,
    owner_module_id: str,
    payload: CrossOwnerFindingSubmission,
) -> None:
    task_id = f"cross-owner-{owner_module_id}-r0-initial"
    attempt_id = f"attempt-legacy-{owner_module_id}"
    correlation = TaskCorrelation(
        workflow_id=f"workflow:{run_id}",
        run_id=run_id,
        task_id=task_id,
        task_attempt_id=attempt_id,
        agent_id="cross-module-reviewer",
        identity_key=f"cross-owner-{owner_module_id}",
        session_id=f"session-{owner_module_id}",
        lease_owner_id=f"test-owner-{owner_module_id}",
        lease_epoch=1,
    )
    runner.service.store.write_json(
        f"Work/runs/{run_id}/task-attempts/{task_id}/current.json",
        correlation.model_dump(mode="json"),
    )
    result = {
        "task_id": task_id,
        "run_id": run_id,
        "agent_id": "cross-module-reviewer",
        "session_id": f"session-{owner_module_id}",
        "status": "completed",
        "payload": payload.model_dump(mode="json"),
    }
    result_ref = (
        f"Work/runs/{run_id}/results/attempts/{task_id}/{attempt_id}.json"
    )
    result_path = runner.service.store.write_json(result_ref, result)
    result_bytes = result_path.read_bytes()
    canonical = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    terminal = TaskTerminal(
        correlation=correlation,
        status="completed",
        result_ref=result_ref,
        result_sha256=hashlib.sha256(result_bytes).hexdigest(),
        payload_sha256=hashlib.sha256(canonical).hexdigest(),
        promoted_to_canonical=True,
        persisted_at_ns=1,
    )
    runner.service.store.write_json(
        result_ref.removesuffix(".json") + ".terminal.json",
        terminal.model_dump(mode="json"),
    )


def test_legacy_cross_owner_promotion_requires_matching_completed_task_payload(
    tmp_path: Path,
) -> None:
    run_id = "run-cross-legacy-binding"
    owner_module_id = "2.1"
    runner = _Runner(tmp_path)
    payload = CrossOwnerFindingSubmission(
        owner_module_id=owner_module_id,
        coverage=CrossReviewCoverageEntry(
            module_id=owner_module_id,
            checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
        ),
        findings=[],
        synthesis_inputs=_synthesis(owner_module_id),
    )
    ref = (
        f"Work/runs/{run_id}/reviews/"
        f"cross-owner-findings-r0-{owner_module_id}.json"
    )
    runner.service.store.write_json(ref, payload.model_dump(mode="json"))
    _write_legacy_completed_task_binding(
        runner,
        run_id=run_id,
        owner_module_id=owner_module_id,
        payload=payload,
    )

    loaded, loaded_ref = lifecycle._load_cross_owner_initial_result(
        runner,
        run_id=run_id,
        owner_module_id=owner_module_id,
        require_task_binding=True,
    )
    assert loaded == payload
    assert loaded_ref == ref

    tampered = payload.model_copy(
        update={"synthesis_inputs": _synthesis("2.2")}
    )
    runner.service.store.write_json(ref, tampered.model_dump(mode="json"))
    with pytest.raises(
        lifecycle.ReviewLifecycleError,
        match="does not match its completed task result",
    ):
        lifecycle._load_cross_owner_initial_result(
            runner,
            run_id=run_id,
            owner_module_id=owner_module_id,
            require_task_binding=True,
        )


@pytest.mark.asyncio
async def test_fixed_five_owner_wave_has_full_owner_views_distinct_sessions_and_exact_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-five"
    runner = _Runner(tmp_path)
    _write_modules(runner, run_id)
    monkeypatch.setattr(
        lifecycle,
        "_verified_cross_owner_noop",
        lambda runner, **kwargs: _fake_noop(runner, **kwargs),
    )
    state = _state(run_id)

    await lifecycle.run_cross_review(runner, state, "workflow-cross-five")

    owner_ids = tuple(REPORT_TAXONOMY)
    assert {owner for owner, _session in runner.calls} == set(owner_ids)
    assert {session for _owner, session in runner.calls} == {
        f"cross-owner-{module_id}" for module_id in owner_ids
    }
    for owner_module_id in owner_ids:
        input_path = (
            tmp_path
            / f"Work/runs/{run_id}/reviews/cross-owner-input-r0-{owner_module_id}.json"
        )
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        assert payload["owner_subject"]["module_id"] == owner_module_id
        assert set(payload["owner_subject"]["submodule_narratives"]) == set(
            REPORT_TAXONOMY[owner_module_id].submodules
        )
        assert set(payload["related_module_views"]) == set(owner_ids) - {owner_module_id}
        assert owner_module_id not in payload["related_module_views"]

    barrier = json.loads(
        (tmp_path / state["cross_owner_barrier_ref"]).read_text(encoding="utf-8")
    )
    assert barrier["target_modules"] == list(owner_ids)
    assert set(barrier["completion_refs"]) == set(owner_ids)
    assert barrier["completion_revisions"] == {module_id: 0 for module_id in owner_ids}
    assert (tmp_path / f"Work/runs/{run_id}/reviews/cross-completion.json").is_file()

    workflow_runner = object.__new__(ReportWorkflowRunner)
    workflow_runner.service = runner.service
    pack = workflow_runner._materialize_chief_cross_decision_pack(state)
    assert pack.run_id == run_id
    assert pack.module_ids == list(owner_ids)
    assert state["cross_decision_pack_ref"].endswith("cross-decision-pack.json")

    # Recovery is business-state based.  A byte-only formatting change that
    # leaves the typed JSON readable and in-scope must not replay paid lanes.
    initial_result_path = (
        tmp_path
        / f"Work/runs/{run_id}/reviews/cross-owner-findings-r0-2.1.json"
    )
    initial_result_path.write_text(
        initial_result_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    calls_before_tamper_check = len(runner.calls)
    state["resume"] = True
    await lifecycle.run_cross_review(runner, state, "workflow-cross-tamper")
    assert len(runner.calls) == calls_before_tamper_check


@pytest.mark.asyncio
async def test_cross_owner_business_barrier_rejects_corrupt_or_wrong_identity_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-business-barrier"
    runner = _Runner(tmp_path)
    _write_modules(runner, run_id)
    monkeypatch.setattr(
        lifecycle,
        "_verified_cross_owner_noop",
        lambda runner, **kwargs: _fake_noop(runner, **kwargs),
    )
    state = _state(run_id)
    await lifecycle.run_cross_review(runner, state, "workflow-cross-business-barrier")

    barrier_path = tmp_path / state["cross_owner_barrier_ref"]
    barrier = lifecycle.CrossOwnerBarrier.model_validate_json(
        barrier_path.read_text(encoding="utf-8")
    )
    owner_id = "2.1"
    completion_ref = barrier.completion_refs[owner_id]
    completion_path = tmp_path / completion_ref
    original_completion = CrossOwnerCompletion.model_validate_json(
        completion_path.read_text(encoding="utf-8")
    )

    initial_ref = original_completion.initial_result.ref
    initial_path = tmp_path / initial_ref
    initial_bytes = initial_path.read_bytes()
    initial_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(lifecycle.ReviewLifecycleError, match="invalid JSON"):
        lifecycle.verify_cross_owner_barrier(
            runner,
            barrier,
            barrier_path=barrier_path,
            expected_run_id=run_id,
            expected_round=1,
            expected_modules=set(REPORT_TAXONOMY),
        )
    initial_path.write_bytes(initial_bytes)

    # Declared ArtifactRef hash/size are legacy forensic metadata.  Changing
    # only those fields must not invalidate an otherwise typed, in-scope lane.
    assert original_completion.subject is not None
    metadata_only_subject = original_completion.subject.model_copy(
        update={"sha256": "0" * 64, "size": 0}
    )
    metadata_only = original_completion.model_copy(
        update={"subject": metadata_only_subject}
    )
    runner.service.store.write_json(
        completion_ref,
        metadata_only.model_dump(mode="json"),
    )
    lifecycle.verify_cross_owner_barrier(
        runner,
        barrier,
        barrier_path=barrier_path,
        expected_run_id=run_id,
        expected_round=1,
        expected_modules=set(REPORT_TAXONOMY),
    )
    runner.service.store.write_json(
        completion_ref,
        original_completion.model_dump(mode="json"),
    )

    wrong_run = original_completion.model_copy(update={"run_id": "other-run"})
    runner.service.store.write_json(completion_ref, wrong_run.model_dump(mode="json"))
    with pytest.raises(lifecycle.ReviewLifecycleError, match="business identity"):
        lifecycle.verify_cross_owner_barrier(
            runner,
            barrier,
            barrier_path=barrier_path,
            expected_run_id=run_id,
            expected_round=1,
            expected_modules=set(REPORT_TAXONOMY),
        )
    runner.service.store.write_json(
        completion_ref,
        original_completion.model_dump(mode="json"),
    )

    assert original_completion.subject is not None
    wrong_owner_subject = original_completion.subject.model_copy(
        update={"ref": f"Work/runs/{run_id}/modules/2.2-r0.json"}
    )
    wrong_owner = original_completion.model_copy(
        update={"subject": wrong_owner_subject}
    )
    runner.service.store.write_json(completion_ref, wrong_owner.model_dump(mode="json"))
    with pytest.raises(lifecycle.ReviewLifecycleError, match="ownership mismatch"):
        lifecycle.verify_cross_owner_barrier(
            runner,
            barrier,
            barrier_path=barrier_path,
            expected_run_id=run_id,
            expected_round=1,
            expected_modules=set(REPORT_TAXONOMY),
        )
    runner.service.store.write_json(
        completion_ref,
        original_completion.model_dump(mode="json"),
    )

    wrong_revision_subject = original_completion.subject.model_copy(
        update={"ref": f"Work/runs/{run_id}/modules/{owner_id}-r99.json"}
    )
    wrong_revision = original_completion.model_copy(
        update={"subject": wrong_revision_subject}
    )
    runner.service.store.write_json(
        completion_ref,
        wrong_revision.model_dump(mode="json"),
    )
    with pytest.raises(lifecycle.ReviewLifecycleError, match="unreadable"):
        lifecycle.verify_cross_owner_barrier(
            runner,
            barrier,
            barrier_path=barrier_path,
            expected_run_id=run_id,
            expected_round=1,
            expected_modules=set(REPORT_TAXONOMY),
        )


@pytest.mark.asyncio
async def test_cross_owner_recheck_regression_enters_next_revision_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-regression"
    first = _cross_finding("XMR-2.1-001")
    regression = _cross_finding("XMR-2.1-r1-001")

    class _RegressionRunner(_Runner):
        async def _agent(
            self,
            _agent_id,
            envelope,
            _artifacts,
            _workflow_id,
            *,
            session_key=None,
        ):
            owner_module_id = envelope.task_id.split("-")[2]
            self.calls.append((envelope.task_id, session_key or ""))
            if envelope.task_id.endswith("initial"):
                return CrossOwnerFindingSubmission(
                    owner_module_id=owner_module_id,
                    coverage=CrossReviewCoverageEntry(
                        module_id=owner_module_id,
                        checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                    ),
                    findings=[first] if owner_module_id == "2.1" else [],
                    synthesis_inputs=_synthesis(owner_module_id),
                )
            contract = CrossOwnerInput.model_validate_json(
                (self.service.workspace / envelope.input_refs[0]).read_text(
                    encoding="utf-8"
                )
            )
            return CrossOwnerVerdictSubmission(
                owner_module_id=owner_module_id,
                coverage=CrossReviewCoverageEntry(
                    module_id=owner_module_id,
                    checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                ),
                verdicts=[
                    ResolutionVerdict(
                        finding_id=finding.id,
                        verdict="resolved",
                        reason="责任模块当前修订满足该finding的全部reviewer checks。",
                        evidence_refs=[contract.owner_subject_ref],
                    )
                    for finding in contract.required_findings
                ],
                new_findings=(
                    [regression]
                    if owner_module_id == "2.1" and contract.review_round == 1
                    else []
                ),
            )

    runner = _RegressionRunner(tmp_path)
    _write_modules(runner, run_id)
    prior_completion_refs: list[tuple[int, str | None]] = []

    async def tracked_revision_lane(*args, **kwargs):
        prior_completion_refs.append(
            (kwargs["review_round"], kwargs.get("prior_completion_ref"))
        )
        return await _fake_revision_lane(*args, **kwargs)

    monkeypatch.setattr(
        lifecycle,
        "_verified_cross_owner_noop",
        lambda runner, **kwargs: _fake_noop(runner, **kwargs),
    )
    monkeypatch.setattr(lifecycle, "_run_cross_owner_lane", tracked_revision_lane)
    state = _state(run_id)

    await lifecycle.run_cross_review(
        runner, state, "workflow-cross-regression-loop"
    )

    assert (
        tmp_path
        / f"Work/runs/{run_id}/reviews/cross-owner-input-r2-2.1.json"
    ).is_file()
    assert (
        tmp_path
        / f"Work/runs/{run_id}/reviews/cross-owner-verdicts-r2-2.1.json"
    ).is_file()
    aggregate_findings = json.loads(
        (
            tmp_path / f"Work/runs/{run_id}/reviews/cross-findings-r0.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["id"] for item in aggregate_findings["findings"]} == {
        first.id,
        regression.id,
    }
    aggregate_verdicts = json.loads(
        (
            tmp_path / f"Work/runs/{run_id}/reviews/cross-verdicts-r1.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["finding_id"] for item in aggregate_verdicts["verdicts"]} == {
        first.id,
        regression.id,
    }
    assert state["module_submissions"]["2.1"].revision == 2
    assert prior_completion_refs == [
        (1, None),
        (
            2,
            f"Work/runs/{run_id}/reviews/module/cross-r1/2.1/completion.json",
        ),
    ]
    assert [
        task_id
        for task_id, session_key in runner.calls
        if session_key == "cross-owner-2.1"
    ] == [
        "cross-owner-2.1-r0-initial",
        "cross-owner-2.1-r1-recheck",
        "cross-owner-2.1-r2-recheck",
    ]

    # The Cross breakpoint is terminal only after every internal owner round
    # has closed.  Re-entering from that breakpoint must validate and reuse the
    # promoted r2 owner completion even though its lane trigger is the prior
    # r1 verdict, not the original r0 CrossOwnerInput.
    calls_before_resume = len(runner.calls)
    resumed_state = _state(run_id)
    resumed_state["resume"] = True
    await lifecycle.run_cross_review(
        runner, resumed_state, "workflow-cross-regression-resume"
    )

    assert len(runner.calls) == calls_before_resume
    assert resumed_state["module_submissions"]["2.1"].revision == 2
    assert resumed_state["cross_review_completion_ref"] == (
        f"Work/runs/{run_id}/reviews/cross-completion.json"
    )

    pipeline_ref = (
        f"Work/runs/{run_id}/lanes/cross-r1/module-2.1/pipeline-completion.json"
    )
    pipeline = CrossOwnerCompletion.model_validate_json(
        (tmp_path / pipeline_ref).read_text(encoding="utf-8")
    )
    final_verdict_ref = (
        f"Work/runs/{run_id}/reviews/cross-owner-verdicts-r2-2.1.json"
    )
    mismatched_trigger = pipeline.model_copy(
        update={"owner_input": _artifact_ref(runner, final_verdict_ref)}
    )
    with pytest.raises(
        lifecycle.ReviewLifecycleError,
        match="promoted completion has inconsistent rounds",
    ):
        lifecycle._validate_cross_owner_completion_business_identity(
            runner,
            mismatched_trigger,
            run_id=run_id,
            owner_module_id="2.1",
            review_round=1,
        )


@pytest.mark.asyncio
async def test_cross_owner_initial_failure_drains_all_five_and_writes_no_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-five-failure"
    runner = _Runner(tmp_path, failed_owner="2.3")
    _write_modules(runner, run_id)
    monkeypatch.setattr(
        lifecycle,
        "_verified_cross_owner_noop",
        lambda runner, **kwargs: _fake_noop(runner, **kwargs),
    )
    state = _state(run_id)

    with pytest.raises(RuntimeError, match="injected owner failure: 2.3"):
        await lifecycle.run_cross_review(runner, state, "workflow-cross-five-failure")

    owner_ids = tuple(REPORT_TAXONOMY)
    assert {owner for owner, _session in runner.calls} == set(owner_ids)
    terminal_path = (
        tmp_path / f"Work/runs/{run_id}/lanes/cross-r1/owner-terminal.json"
    )
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    assert terminal["target_modules"] == list(owner_ids)
    assert terminal["status"] == "failed"
    assert terminal["terminal_statuses"]["2.3"] == "failed"
    assert {
        module_id: status
        for module_id, status in terminal["terminal_statuses"].items()
        if module_id != "2.3"
    } == {module_id: "completed" for module_id in owner_ids if module_id != "2.3"}
    assert terminal["retry_scope"] == ["2.3"]
    assert set(terminal["completion_refs"]) == set(owner_ids) - {"2.3"}
    assert not (
        tmp_path / f"Work/runs/{run_id}/lanes/cross-r1/owner-barrier.json"
    ).exists()

    workflow_runner = object.__new__(ReportWorkflowRunner)
    workflow_runner.service = runner.service
    # The workflow-level resume guard validates the failure evidence but does
    # not reject a valid owner-scoped retry plan.
    workflow_runner._guard_failed_cross_owner_terminal_resume(run_id=run_id)

    # Explicit resume reuses the four verified promoted completions and calls
    # only the failed owner.  It then commits the exact-five barrier and
    # continues from the merged result.
    runner.failed_owner = None
    calls_before_resume = len(runner.calls)
    state["resume"] = True
    await lifecycle.run_cross_review(runner, state, "workflow-cross-five-resume")

    assert runner.calls[calls_before_resume:] == [("2.3", "cross-owner-2.3")]
    barrier = json.loads(
        (
            tmp_path
            / f"Work/runs/{run_id}/lanes/cross-r1/owner-barrier.json"
        ).read_text(encoding="utf-8")
    )
    assert set(barrier["completion_refs"]) == set(owner_ids)
    assert (
        tmp_path / f"Work/runs/{run_id}/reviews/cross-completion.json"
    ).is_file()


@pytest.mark.asyncio
async def test_fast_cross_owner_enters_local_pipeline_without_waiting_for_slow_initial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-owner-independent-pipelines"
    slow_release = asyncio.Event()
    fast_promoted = asyncio.Event()

    class _StaggeredRunner(_Runner):
        async def _agent(
            self,
            _agent_id,
            envelope,
            _artifacts,
            _workflow_id,
            *,
            session_key=None,
        ):
            owner_module_id = envelope.task_id.split("-")[2]
            self.calls.append((owner_module_id, session_key or ""))
            if owner_module_id == "2.5":
                await slow_release.wait()
            return CrossOwnerFindingSubmission(
                owner_module_id=owner_module_id,
                coverage=CrossReviewCoverageEntry(
                    module_id=owner_module_id,
                    checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                ),
                findings=[],
                synthesis_inputs=_synthesis(owner_module_id),
            )

    runner = _StaggeredRunner(tmp_path)
    _write_modules(runner, run_id)

    def _recording_noop(runner, **kwargs):
        result = _fake_noop(runner, **kwargs)
        if kwargs["owner_module_id"] == "2.1":
            fast_promoted.set()
        return result

    monkeypatch.setattr(lifecycle, "_verified_cross_owner_noop", _recording_noop)
    task = asyncio.create_task(
        lifecycle.run_cross_review(runner, _state(run_id), "workflow-cross-independent")
    )
    await asyncio.wait_for(fast_promoted.wait(), timeout=1)
    assert not task.done()
    slow_release.set()
    await task


@pytest.mark.asyncio
async def test_declarative_cross_owner_cohort_preserves_independent_pipeline_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-declarative-cohort"
    slow_release = asyncio.Event()
    fast_promoted = asyncio.Event()

    class _StaggeredRunner(_Runner):
        async def _agent(
            self,
            _agent_id,
            envelope,
            _artifacts,
            _workflow_id,
            *,
            session_key=None,
        ):
            owner_module_id = envelope.task_id.split("-")[2]
            self.calls.append((owner_module_id, session_key or ""))
            if owner_module_id == "2.5":
                await slow_release.wait()
            return CrossOwnerFindingSubmission(
                owner_module_id=owner_module_id,
                coverage=CrossReviewCoverageEntry(
                    module_id=owner_module_id,
                    checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                ),
                findings=[],
                synthesis_inputs=_synthesis(owner_module_id),
            )

    declarative_root = tmp_path / "declarative-cross"
    declarative_runner = _StaggeredRunner(declarative_root)
    _write_modules(declarative_runner, run_id)

    def _recording_noop(runner, **kwargs):
        result = _fake_noop(runner, **kwargs)
        if kwargs["owner_module_id"] == "2.1":
            fast_promoted.set()
        return result

    monkeypatch.setattr(lifecycle, "_verified_cross_owner_noop", _recording_noop)
    declarative_task = asyncio.create_task(
        _execute_declarative_cross_owner_cohort(
            declarative_runner,
            _state(run_id),
            "workflow-cross-declarative-cohort",
        )
    )
    await asyncio.wait_for(fast_promoted.wait(), timeout=1)
    assert not declarative_task.done()
    slow_release.set()
    declarative_result, declarative_state = await declarative_task

    owner_ids = tuple(REPORT_TAXONOMY)
    branch_results = declarative_state.parallel_results["cross-owner-cohort"]
    assert set(branch_results) == set(owner_ids)
    assert all(
        f"outcome-{owner_module_id}" in branch_results[owner_module_id]
        for owner_module_id in owner_ids
    )
    assert {
        (owner_module_id, session_key)
        for owner_module_id, session_key in declarative_runner.calls
    } == {
        (owner_module_id, f"cross-owner-{owner_module_id}")
        for owner_module_id in owner_ids
    }

    legacy_root = tmp_path / "legacy-cross"
    legacy_runner = _Runner(legacy_root)
    _write_modules(legacy_runner, run_id)
    legacy_state = _state(run_id)
    await lifecycle.run_cross_review(
        legacy_runner,
        legacy_state,
        "workflow-cross-legacy-comparison",
    )

    def _module_projection(value: dict) -> dict[str, dict]:
        return {
            module_id: ModuleSubmission.model_validate(module).model_dump(mode="json")
            for module_id, module in value.items()
        }

    assert declarative_result["cross_review_completion_ref"] == (
        legacy_state["cross_review_completion_ref"]
    )
    assert declarative_result["cross_owner_barrier_ref"] == (
        legacy_state["cross_owner_barrier_ref"]
    )
    assert _module_projection(declarative_result["module_submissions"]) == (
        _module_projection(legacy_state["module_submissions"])
    )
    assert [
        CrossSynthesisInput.model_validate(item).model_dump(mode="json")
        for item in declarative_result["cross_synthesis_inputs"]
    ] == [
        CrossSynthesisInput.model_validate(item).model_dump(mode="json")
        for item in legacy_state["cross_synthesis_inputs"]
    ]


@pytest.mark.asyncio
async def test_declarative_cross_owner_initial_reviewer_uses_agent_port_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-declarative-initial-agent"
    runner = _Runner(tmp_path)
    _write_modules(runner, run_id)
    monkeypatch.setattr(
        lifecycle,
        "_verified_cross_owner_noop",
        lambda runner, **kwargs: _fake_noop(runner, **kwargs),
    )

    completed = await _execute_declarative_cross_owner_pipeline(
        runner,
        _state(run_id),
        "workflow-cross-declarative-initial-agent",
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.calls == [("2.1", "cross-owner-2.1")]
    conversation = completed.conversations["cross-owner-conversation"]
    assert conversation.key.agent_id == "cross-module-reviewer"
    assert conversation.key.value == "cross-owner-2.1"
    result = DeclarativeCrossOwnerPipelineOutcome.model_validate(
        completed.outputs["result"]
    )
    assert result.owner_module_id == "2.1"
    assert result.status == "completed"
    assert result.pipeline is not None


@pytest.mark.asyncio
async def test_declarative_cross_owner_initial_result_recovery_skips_agent_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-declarative-initial-recovery"
    runner = _Runner(tmp_path)
    _write_modules(runner, run_id)
    monkeypatch.setattr(
        lifecycle,
        "_verified_cross_owner_noop",
        lambda runner, **kwargs: _fake_noop(runner, **kwargs),
    )
    state = _state(run_id)
    _owner_input, _owner_input_ref = lifecycle._cross_owner_input(
        runner,
        state=state,
        modules=state["module_submissions"],
        owner_module_id="2.1",
        phase="initial",
        review_round=0,
    )
    initial_result = CrossOwnerFindingSubmission(
        owner_module_id="2.1",
        coverage=CrossReviewCoverageEntry(
            module_id="2.1",
            checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
        ),
        findings=[],
        synthesis_inputs=_synthesis("2.1"),
    )
    runner.service.store.write_json(
        f"Work/runs/{run_id}/reviews/cross-owner-findings-r0-2.1.json",
        initial_result.model_dump(mode="json"),
    )

    completed = await _execute_declarative_cross_owner_pipeline(
        runner,
        state,
        "workflow-cross-declarative-initial-recovery",
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.calls == []
    assert "cross-owner-conversation" not in completed.conversations
    result = DeclarativeCrossOwnerPipelineOutcome.model_validate(
        completed.outputs["result"]
    )
    assert result.owner_module_id == "2.1"
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_declarative_cross_owner_finding_invokes_original_author_once_before_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterize the next Cross owner finding-to-author boundary."""

    run_id = "run-cross-declarative-finding-revision"
    runner = _CrossFindingRunner(tmp_path)
    _write_modules(runner, run_id)
    state = _state(run_id)
    _write_initial_module_completion(runner, state, "2.1")
    compatibility: dict[str, object] = {}

    async def _record_compatibility_lane(runner, **kwargs):
        compatibility.update(kwargs)
        return _fake_noop(
            runner,
            state=kwargs["state"],
            owner_module_id=kwargs["module_id"],
            module=kwargs["accepted_local_review"].review.current,
            owner_input_ref=kwargs["owner_input_ref"],
            review_round=kwargs["review_round"],
        )

    async def _resolved_recheck(
        runner,
        *,
        state,
        owner_module_id,
        review_round,
        required_findings=None,
        **kwargs,
    ):
        del kwargs
        findings = list(required_findings or [])
        verdict = CrossOwnerVerdictSubmission(
            owner_module_id=owner_module_id,
            coverage=CrossReviewCoverageEntry(
                module_id=owner_module_id,
                checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
            ),
            verdicts=[
                ResolutionVerdict(
                    finding_id=finding.id,
                    verdict="resolved",
                    reason="当前接受的模块修订已经满足 Cross owner 的全部检查项。",
                    evidence_refs=[
                        f"Work/runs/{state['run_id']}/modules/{owner_module_id}-r1.json"
                    ],
                )
                for finding in findings
            ],
            new_findings=[],
        )
        ref = (
            f"Work/runs/{state['run_id']}/reviews/"
            f"cross-owner-verdicts-r{review_round}-{owner_module_id}.json"
        )
        runner.service.store.write_json(ref, verdict.model_dump(mode="json"))
        return verdict, ref

    monkeypatch.setattr(lifecycle, "_run_cross_owner_lane", _record_compatibility_lane)
    monkeypatch.setattr(lifecycle, "_run_cross_owner_review", _resolved_recheck)

    completed = await _execute_declarative_cross_owner_pipeline(
        runner,
        state,
        "workflow-cross-declarative-finding-revision",
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.agent_calls == [
        ("cross-module-reviewer", "cross-owner-2.1-r0-initial", "cross-owner-2.1"),
        ("module-2.1-specialist", "module-revision-r1-2.1", "module-2.1"),
        (
            "evidence-auditor",
            "module-2.1-cross-r1-review-r0",
            "module-auditor-2.1",
        ),
    ]
    assert compatibility["accepted_revision"].revised.revision == 1
    local_acceptance = compatibility["accepted_local_review"]
    assert local_acceptance.review.current.revision == 1
    assert local_acceptance.review.next_action == "completed"
    local_input = local_acceptance.regression_context
    assert local_input.trigger_cross_findings[0].id == "XMR-2.1-001"
    assert local_input.trigger_revision_responses[0].finding_id == "XMR-2.1-001"
    persisted_input = runner.local_review_inputs[0]
    assert persisted_input.phase == "local_regression"
    assert persisted_input.baseline_subject_ref.endswith("/modules/2.1-r0.json")
    assert persisted_input.subject_revision == 1
    assert persisted_input.required_submodule_ids == ["2.1.1"]
    assert persisted_input.trigger_cross_findings[0].id == "XMR-2.1-001"
    assert persisted_input.trigger_revision_responses[0].finding_id == "XMR-2.1-001"
    assert persisted_input.revision_diff.from_revision == 0
    assert persisted_input.revision_diff.to_revision == 1
    conversation_values = {
        record.key.value for record in completed.conversations.values()
    }
    assert conversation_values >= {
        "cross-owner-2.1",
        "module-2.1",
        "module-auditor-2.1",
    }


@pytest.mark.asyncio
async def test_declarative_cross_owner_persisted_revision_skips_author_and_reuses_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved higher revision is the owner continuation input."""

    run_id = "run-cross-declarative-candidate-revision"
    runner = _CrossFindingRunner(tmp_path)
    _write_modules(runner, run_id)
    state = _state(run_id)
    _owner_input, owner_input_ref = lifecycle._cross_owner_input(
        runner,
        state=state,
        modules=state["module_submissions"],
        owner_module_id="2.1",
        phase="initial",
        review_round=0,
    )
    initial_result = CrossOwnerFindingSubmission(
        owner_module_id="2.1",
        coverage=CrossReviewCoverageEntry(
            module_id="2.1",
            checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
        ),
        findings=[_cross_finding("XMR-2.1-001")],
        synthesis_inputs=_synthesis("2.1"),
    )
    runner.service.store.write_json(
        f"Work/runs/{run_id}/reviews/cross-owner-findings-r0-2.1.json",
        initial_result.model_dump(mode="json"),
    )

    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    candidate = _module("2.1", revision=1).model_copy(
        update={
            "submodule_narratives": {
                **_module("2.1").submodule_narratives,
                target: f"{target} revised for the persisted Cross finding",
            },
            "revision_responses": [
                RevisionResponse(
                    finding_id="XMR-2.1-001",
                    action="implemented",
                    summary="已按 Cross owner finding 补充模块接口与联合验证条件。",
                    changed_target_ids=[target],
                )
            ],
        }
    )
    candidate_ref = f"Work/runs/{run_id}/modules/2.1-r1.json"
    runner.service.store.write_json(candidate_ref, candidate.model_dump(mode="json"))

    completion_ref = f"Work/runs/{run_id}/reviews/module/initial/2.1/completion.json"
    runner.service.store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.1",
            subject_refs=[f"Work/runs/{run_id}/modules/2.1-r0.json"],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )
    state["module_review_completion_refs"]["2.1"] = completion_ref

    async def _unexpected_legacy_local_review(*_args, **_kwargs):
        raise AssertionError("accepted local review must not replay Legacy Auditor")

    monkeypatch.setattr(lifecycle, "run_module_review", _unexpected_legacy_local_review)

    completed = await _execute_declarative_cross_owner_pipeline(
        runner,
        state,
        "workflow-cross-declarative-candidate-revision",
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert [agent_id for agent_id, _task, _session in runner.agent_calls] == [
        "evidence-auditor",
        "cross-module-reviewer",
    ]
    assert runner.agent_calls[0] == (
        "evidence-auditor",
        "module-2.1-cross-r1-review-r0",
        "module-auditor-2.1",
    )
    assert all(
        record.key.value != "module-2.1"
        for record in completed.conversations.values()
    )
    assert any(
        record.key.value == "module-auditor-2.1"
        for record in completed.conversations.values()
    )
    pipeline = DeclarativeCrossOwnerPipelineOutcome.model_validate(
        completed.outputs["result"]
    ).pipeline
    assert pipeline is not None
    assert pipeline["lane"]["module"]["revision"] == 1
    assert pipeline["lane"]["completion"]["subject_revision"] == 1
    assert owner_input_ref in pipeline["lane"]["completion"]["owner_input"]["ref"]


@pytest.mark.asyncio
async def test_declarative_cross_owner_revision_failure_retries_only_failed_owner_author(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-declarative-revision-retry"
    workflow_id = "workflow-cross-declarative-revision-retry"
    finding = _cross_finding("XMR-2.3-001").model_copy(
        update={"owner_module_id": "2.3", "target_submodule_ids": ["2.3.1"]}
    )

    class _RevisionFailureRunner(_Runner):
        def __init__(self, workspace: Path) -> None:
            super().__init__(workspace)
            self.fail_author = True
            self.fail_local_auditor = True
            self.agent_calls: list[tuple[str, str, str]] = []

        async def _agent(
            self,
            agent_id,
            envelope,
            _artifacts,
            _workflow_id,
            *,
            session_key=None,
        ):
            session = session_key or ""
            self.agent_calls.append((agent_id, envelope.task_id, session))
            if agent_id == "cross-module-reviewer":
                owner_module_id = envelope.task_id.split("-")[2]
                if envelope.task_id.endswith("initial"):
                    return CrossOwnerFindingSubmission(
                        owner_module_id=owner_module_id,
                        coverage=CrossReviewCoverageEntry(
                            module_id=owner_module_id,
                            checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                        ),
                        findings=[finding] if owner_module_id == "2.3" else [],
                        synthesis_inputs=_synthesis(owner_module_id),
                    )
                contract = CrossOwnerInput.model_validate_json(
                    (self.service.workspace / envelope.input_refs[0]).read_text(
                        encoding="utf-8"
                    )
                )
                return CrossOwnerVerdictSubmission(
                    owner_module_id=owner_module_id,
                    coverage=CrossReviewCoverageEntry(
                        module_id=owner_module_id,
                        checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                    ),
                    verdicts=[
                        ResolutionVerdict(
                            finding_id=item.id,
                            verdict="resolved",
                            reason="重试后的原 Author 修订满足 Cross owner 检查项。",
                            evidence_refs=[contract.owner_subject_ref],
                        )
                        for item in contract.required_findings
                    ],
                    new_findings=[],
                )
            if agent_id == "module-2.3-specialist":
                if self.fail_author:
                    raise RuntimeError("injected Cross owner Author failure: 2.3")
                return ModuleRevisionSubmission(
                    module_id="2.3",
                    base_revision=0,
                    revision=1,
                    submodule_narratives={
                        "2.3.1": "2.3.1 revised for the Cross owner finding"
                    },
                    source_ids=[],
                    unresolved_questions=[],
                    revision_responses=[
                        RevisionResponse(
                            finding_id=finding.id,
                            action="implemented",
                            summary=(
                                "已补充模块接口责任、实施顺序、联合验证步骤和明确关闭条件。"
                            ),
                            changed_target_ids=["2.3.1"],
                        )
                    ],
                )
            if agent_id == "evidence-auditor":
                if self.fail_local_auditor:
                    raise RuntimeError(
                        "injected Cross owner local Auditor failure: 2.3"
                    )
                review_input = lifecycle.ModuleReviewInput.model_validate_json(
                    (self.service.workspace / envelope.input_refs[0]).read_text(
                        encoding="utf-8"
                    )
                )
                return ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": review_input.required_submodule_ids},
                    findings=[],
                )
            raise AssertionError(f"unexpected Agent invocation: {agent_id}")

        def _validate_module_structure(self, state, module, phase):
            return _CrossFindingRunner._validate_module_structure(
                self,
                state,
                module,
                phase,
            )

        @staticmethod
        def _template_skill_context(_state, *_parts):
            return ""

    runner = _RevisionFailureRunner(tmp_path)
    _write_modules(runner, run_id)

    async def _accepted_revision_lane(runner, **kwargs):
        accepted = kwargs["accepted_revision"]
        result = _fake_noop(
            runner,
            state=kwargs["state"],
            owner_module_id=kwargs["module_id"],
            module=accepted.revised,
            owner_input_ref=kwargs["owner_input_ref"],
            review_round=kwargs["review_round"],
        )
        return result.model_copy(
            update={"responses": accepted.revised.revision_responses}
        )

    monkeypatch.setattr(lifecycle, "_run_cross_owner_lane", _accepted_revision_lane)
    monkeypatch.setattr(
        lifecycle,
        "_verified_cross_owner_noop",
        lambda runner, **kwargs: _fake_noop(runner, **kwargs),
    )
    definitions, contracts, _tail = build_reporting_tail_definition()
    executors = build_builtin_executor_registry()
    cohort_plan, pipeline_plans = compile_cross_owner_workflows(
        definitions,
        executors,
    )
    state_store = FileWorkflowStateStore(tmp_path)

    def _context(runtime: DeclarativeCrossOwnerRuntime) -> RuntimeContext:
        return RuntimeContext(
            tools={
                "prepare-cross-owner-cohort": runtime.prepare,
                "prepare-current-cross-owner-initial": runtime.prepare_initial,
                "cross-owner-initial-requires-agent": runtime.initial_requires_agent,
                "accept-current-cross-owner-initial": runtime.accept_initial,
                "cross-owner-initial-has-findings": runtime.initial_has_findings,
                "prepare-current-cross-owner-revision": runtime.prepare_revision,
                "cross-owner-revision-requires-agent": runtime.revision_requires_agent,
                "accept-current-cross-owner-revision": runtime.accept_revision,
                "prepare-current-cross-owner-local-review": (
                    runtime.prepare_local_review
                ),
                "cross-owner-local-review-requires-agent": (
                    runtime.local_review_requires_agent
                ),
                "accept-current-cross-owner-local-review": (
                    runtime.accept_local_review
                ),
                "continue-current-cross-owner-pipeline": runtime.continue_owner,
                "reduce-cross-owner-cohort": runtime.reduce,
            },
            agents=runtime.agent_invokers,
            contracts=contracts,
            definitions=definitions,
            subworkflows=pipeline_plans,
        )

    reporting_state = _state(run_id)
    _write_initial_module_completion(runner, reporting_state, "2.3")
    runtime = DeclarativeCrossOwnerRuntime(runner, reporting_state, workflow_id)
    kernel_state = WorkflowState.for_plan(run_id, cohort_plan)
    kernel_state.variables["reporting-state"] = reporting_state
    host = WorkflowRuntimeHost(
        executors,
        state_store,
        InMemoryWorkflowEventSink(),
    )

    with pytest.raises(RuntimeError, match="injected Cross owner Author failure"):
        await host.execute(cohort_plan, kernel_state, _context(runtime))

    failed_state = state_store.load(run_id)
    outcomes = {
        owner_module_id: DeclarativeCrossOwnerPipelineOutcome.model_validate(
            failed_state.parallel_results["cross-owner-cohort"][owner_module_id][
                f"outcome-{owner_module_id}"
            ]
        )
        for owner_module_id in REPORT_TAXONOMY
    }
    assert outcomes["2.3"].status == "failed"
    assert all(
        outcome.status == "completed"
        for owner_module_id, outcome in outcomes.items()
        if owner_module_id != "2.3"
    )
    initial_task_id = "cross-owner-2.3-r0-initial"
    assert sum(task_id == initial_task_id for _agent, task_id, _session in runner.agent_calls) == 1
    calls_before_retry = list(runner.agent_calls)

    runner.fail_author = False
    retry_state = retry_failed_cross_owner_pipelines(cohort_plan, failed_state)
    retry_runtime = DeclarativeCrossOwnerRuntime(
        runner,
        retry_state.variables["prepared-cross-state"],
        workflow_id,
    )
    with pytest.raises(RuntimeError, match="local Auditor failure"):
        await host.execute(
            cohort_plan,
            retry_state,
            _context(retry_runtime),
        )

    local_failed_state = state_store.load(run_id)
    local_outcomes = {
        owner_module_id: DeclarativeCrossOwnerPipelineOutcome.model_validate(
            local_failed_state.parallel_results["cross-owner-cohort"][
                owner_module_id
            ][f"outcome-{owner_module_id}"]
        )
        for owner_module_id in REPORT_TAXONOMY
    }
    assert local_outcomes["2.3"].status == "failed"
    assert all(
        outcome.status == "completed"
        for owner_module_id, outcome in local_outcomes.items()
        if owner_module_id != "2.3"
    )
    author_retry_calls = runner.agent_calls[len(calls_before_retry) :]
    assert author_retry_calls[0] == (
        "module-2.3-specialist",
        "module-revision-r1-2.3",
        "module-2.3",
    )
    assert author_retry_calls[1] == (
        "evidence-auditor",
        "module-2.3-cross-r1-review-r0",
        "module-auditor-2.3",
    )

    calls_before_local_retry = list(runner.agent_calls)
    runner.fail_local_auditor = False
    local_retry_state = retry_failed_cross_owner_pipelines(
        cohort_plan,
        local_failed_state,
    )
    local_retry_runtime = DeclarativeCrossOwnerRuntime(
        runner,
        local_retry_state.variables["prepared-cross-state"],
        workflow_id,
    )
    recovered = await host.execute(
        cohort_plan,
        local_retry_state,
        _context(local_retry_runtime),
    )

    assert recovered.status is WorkflowStatus.COMPLETED
    retry_calls = runner.agent_calls[len(calls_before_local_retry) :]
    assert retry_calls[0] == (
        "evidence-auditor",
        "module-2.3-cross-r1-review-r0",
        "module-auditor-2.3",
    )
    assert all(
        agent_id in {"evidence-auditor", "cross-module-reviewer"}
        for agent_id, _task_id, _session in retry_calls
    )
    assert sum(task_id == initial_task_id for _agent, task_id, _session in runner.agent_calls) == 1
    assert sum(
        task_id == "module-revision-r1-2.3"
        for _agent, task_id, _session in runner.agent_calls
    ) == 2


@pytest.mark.asyncio
async def test_declarative_cross_owner_accepted_initial_reuses_result_on_resume_without_task_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-declarative-accepted-initial-resume"
    workflow_id = "workflow-cross-declarative-accepted-initial-resume"
    runner = _Runner(tmp_path)
    _write_modules(runner, run_id)
    monkeypatch.setattr(
        lifecycle,
        "_verified_cross_owner_noop",
        lambda runner, **kwargs: _fake_noop(runner, **kwargs),
    )
    state = _state(run_id)
    runtime = DeclarativeCrossOwnerRuntime(runner, state, workflow_id)
    runtime.prepare(state)
    context = runtime.prepare_initial(
        {
            "state": state,
            "owner_module_id": "2.1",
        }
    )
    assert context.status == "initial_ready"
    definitions, _contracts, _tail = build_reporting_tail_definition()
    conversation = ConversationRegistry().create(
        ConversationKey(
            agent_id="cross-module-reviewer",
            value="cross-owner-2.1",
            mode=ConversationMode.RUN,
        ),
        run_id=run_id,
    )
    agent = definitions.require(DefinitionKind.AGENT, "cross-module-reviewer")
    task = definitions.require(
        DefinitionKind.TASK,
        "cross-owner-runtime-initial-review",
    )
    invocation = await runtime.agent_invokers["cross-module-reviewer"].invoke(
        agent,
        task,
        context,
        conversation,
        task_id="invoke-current-cross-owner-initial",
    )
    typed_result = DeclarativeCrossOwnerInitialAgentResult.model_validate(
        invocation.result
    )
    assert typed_result.status == "completed"
    accepted = runtime.accept_initial(
        {
            "context": context,
            "result": typed_result,
        }
    )
    assert accepted.status == "initial_accepted"
    calls_after_accept = list(runner.calls)
    assert calls_after_accept == [("2.1", "cross-owner-2.1")]

    # The accepted typed result is the declarative continuation boundary.  A
    # reconstructed state may carry resume=True without a legacy task binding;
    # continue_owner must use the accepted result and avoid a second Agent turn.
    state["resume"] = True
    legacy_binding = (
        tmp_path
        / f"Work/runs/{run_id}/task-attempts/cross-owner-2.1-r0-initial/current.json"
    )
    assert not legacy_binding.exists()
    completed = await runtime.continue_owner(accepted)

    assert completed.status == "completed"
    assert runner.calls == calls_after_accept


@pytest.mark.asyncio
async def test_declarative_cross_owner_cohort_retries_only_failed_owner_from_file_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-declarative-retry"
    workflow_id = "workflow-cross-declarative-retry"
    owner_ids = tuple(REPORT_TAXONOMY)
    runner = _Runner(tmp_path, failed_owner="2.3")
    _write_modules(runner, run_id)
    monkeypatch.setattr(
        lifecycle,
        "_verified_cross_owner_noop",
        lambda runner, **kwargs: _fake_noop(runner, **kwargs),
    )

    definitions, contracts, _tail = build_reporting_tail_definition()
    executors = build_builtin_executor_registry()
    cohort_plan, pipeline_plans = compile_cross_owner_workflows(
        definitions,
        executors,
    )
    state_store = FileWorkflowStateStore(tmp_path)

    def _context(runtime: DeclarativeCrossOwnerRuntime) -> RuntimeContext:
        return RuntimeContext(
            tools={
                "prepare-cross-owner-cohort": runtime.prepare,
                "prepare-current-cross-owner-initial": runtime.prepare_initial,
                "cross-owner-initial-requires-agent": runtime.initial_requires_agent,
                "accept-current-cross-owner-initial": runtime.accept_initial,
                "cross-owner-initial-has-findings": runtime.initial_has_findings,
                "prepare-current-cross-owner-revision": runtime.prepare_revision,
                "cross-owner-revision-requires-agent": (
                    runtime.revision_requires_agent
                ),
                "accept-current-cross-owner-revision": runtime.accept_revision,
                "prepare-current-cross-owner-local-review": (
                    runtime.prepare_local_review
                ),
                "cross-owner-local-review-requires-agent": (
                    runtime.local_review_requires_agent
                ),
                "accept-current-cross-owner-local-review": (
                    runtime.accept_local_review
                ),
                "continue-current-cross-owner-pipeline": runtime.continue_owner,
                "reduce-cross-owner-cohort": runtime.reduce,
            },
            agents=runtime.agent_invokers,
            contracts=contracts,
            definitions=definitions,
            subworkflows=pipeline_plans,
        )

    initial_reporting_state = _state(run_id)
    initial_runtime = DeclarativeCrossOwnerRuntime(
        runner,
        initial_reporting_state,
        workflow_id,
    )
    initial_kernel_state = WorkflowState.for_plan(run_id, cohort_plan)
    initial_kernel_state.variables["reporting-state"] = initial_reporting_state
    host = WorkflowRuntimeHost(
        executors,
        state_store,
        InMemoryWorkflowEventSink(),
    )

    with pytest.raises(RuntimeError, match="injected owner failure: 2.3"):
        await host.execute(
            cohort_plan,
            initial_kernel_state,
            _context(initial_runtime),
        )

    failed_state = state_store.load(run_id)
    assert failed_state.status is WorkflowStatus.FAILED
    assert set(failed_state.parallel_results["cross-owner-cohort"]) == set(owner_ids)
    assert set(failed_state.parallel_states["cross-owner-cohort"]) == set(owner_ids)
    assert all(
        branch_state["status"] == WorkflowStatus.COMPLETED
        for branch_state in failed_state.parallel_states["cross-owner-cohort"].values()
    )
    outcomes = {
        owner_module_id: DeclarativeCrossOwnerPipelineOutcome.model_validate(
            failed_state.parallel_results["cross-owner-cohort"][owner_module_id][
                f"outcome-{owner_module_id}"
            ]
        )
        for owner_module_id in owner_ids
    }
    assert outcomes["2.3"].status == "failed"
    assert {
        owner_module_id
        for owner_module_id, outcome in outcomes.items()
        if outcome.status == "completed"
    } == set(owner_ids) - {"2.3"}
    assert runner.calls.count(("2.3", "cross-owner-2.3")) == 1
    calls_before_retry = list(runner.calls)

    runner.failed_owner = None
    retry_state = retry_failed_cross_owner_pipelines(cohort_plan, failed_state)
    assert retry_state.status is WorkflowStatus.PENDING
    assert set(retry_state.parallel_results["cross-owner-cohort"]) == set(owner_ids) - {
        "2.3"
    }
    retry_reporting_state = retry_state.variables["prepared-cross-state"]
    retry_runtime = DeclarativeCrossOwnerRuntime(
        runner,
        retry_reporting_state,
        workflow_id,
    )
    recovered = await host.execute(
        cohort_plan,
        retry_state,
        _context(retry_runtime),
    )

    assert recovered.status is WorkflowStatus.COMPLETED
    assert runner.calls[len(calls_before_retry) :] == [
        ("2.3", "cross-owner-2.3")
    ]
    assert set(recovered.parallel_results["cross-owner-cohort"]) == set(owner_ids)
    aggregate = recovered.outputs["result"]
    assert aggregate["cross_review_completion_ref"] == (
        f"Work/runs/{run_id}/reviews/cross-completion.json"
    )
    assert aggregate["cross_owner_barrier_ref"] == (
        f"Work/runs/{run_id}/lanes/cross-r1/owner-barrier.json"
    )
    persisted = state_store.load(run_id)
    assert persisted.status is WorkflowStatus.COMPLETED
