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
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
)
from manyselves.core.reporting.input_contracts import CrossOwnerInput, ReviewCompletionRecord
from manyselves.core.reporting.parallel_runtime import (
    ArtifactRef,
    CrossOwnerCompletion,
    TaskCorrelation,
    TaskTerminal,
)
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.workflow import ReportWorkflowRunner


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

    def _load_current_review_completion(self, **kwargs):
        return ReportWorkflowRunner._load_current_review_completion(self, **kwargs)


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
