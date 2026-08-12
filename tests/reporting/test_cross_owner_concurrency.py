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
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    ModuleSubmission,
    RevisionResponse,
)
from manyselves.core.reporting.input_contracts import ValidationReport
from manyselves.core.reporting.parallel_runtime import (
    ArtifactRef,
    CrossOwnerCompletion,
)
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY


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


def _synthesis() -> list[dict]:
    shared = {
        "related_module_ids": ["2.1", "2.2"],
        "root_causes": ["共同根因"],
        "propagation_steps": ["风险传播路径一", "风险传播路径二"],
        "causal_chain": "跨模块因果链需要把责任边界、触发条件和联合验收逐项写清，确保风险传播和处置顺序可以复核。",
        "decision_implication": "按依赖顺序整改并在联合验收记录中闭环，相关责任接口和剩余边界需要同步保留。",
        "action_dependencies": ["先核查再整改"],
        "joint_actions": ["联合验收"],
        "verification_method": "通过联合试验、记录核对和复测结果完成验收。",
        "acceptance_criteria": ["验收通过"],
        "module_statement_refs": ["2.1.1", "2.2.1"],
        "confidence_and_boundary": "当前证据边界明确，未验证条件必须保留并标注。",
        "target_report_section_ids": ["3.1.1"],
        "evidence_refs": ["E-0001"],
    }
    return [
        {"id": "S-RISK", "cluster_type": "risk_cluster", **shared},
        {"id": "S-GLOBAL", "cluster_type": "global_propagation", **shared},
    ]


class _Runner:
    def __init__(self, workspace: Path, scripted: list[object]) -> None:
        self.service = SimpleNamespace(workspace=workspace, store=ReportingStore(workspace))
        self.scripted = list(scripted)
        self.calls: list[tuple[str, str]] = []

    async def _agent(self, agent_id, envelope, _artifacts, _workflow_id, *, session_key=None):
        self.calls.append((agent_id, session_key or ""))
        return self.scripted.pop(0)

    @staticmethod
    def _user_supplement_constraints(_state, **_kwargs):
        return []

    @staticmethod
    def _role_skill_context(_state, _role, **_kwargs):
        return ""


def _cross_inputs(run_id: str, owner_ids: tuple[str, ...]):
    coverage = [
        {"module_id": module_id, "checked_dimensions": list(CROSS_REVIEW_DIMENSIONS)}
        for module_id in REPORT_TAXONOMY
    ]
    findings = []
    for module_id in owner_ids:
        target = next(iter(REPORT_TAXONOMY[module_id].submodules))
        findings.append(
            {
                "id": f"X-{module_id}",
                "owner_module_id": module_id,
                "target_submodule_ids": [target],
                "related_module_ids": ["2.5"],
                "category": "dependencies",
                "impact": "blocking",
                "observation": "当前正文尚未明确责任接口、实施顺序和联合验收记录，跨模块风险无法闭环。",
                "evidence_refs": [f"Work/runs/{run_id}/modules/{module_id}-r0.json"],
                "required_change": "请在责任模块目标小节补充依赖对象、实施顺序、责任接口和联合验收记录要求。",
                "reviewer_checks": ["接口、顺序和验收均明确"],
                "machine_checks": [],
            }
        )
    verdicts = [
        {
            "finding_id": finding["id"],
            "verdict": "resolved",
            "reason": "责任模块已经完成修订并通过原审查者回归。",
            "evidence_refs": [
                f"Work/runs/{run_id}/modules/{finding['owner_module_id']}-r1.json"
            ],
        }
        for finding in findings
    ]
    return (
        CrossReviewFindingSubmission(
            coverage=coverage,
            findings=findings,
            synthesis_inputs=_synthesis(),
        ),
        CrossReviewVerdictSubmission(
            coverage=coverage,
            verdicts=verdicts,
            new_findings=[],
            synthesis_inputs=_synthesis(),
        ),
        findings,
    )


def _write_completion(
    runner: _Runner,
    *,
    run_id: str,
    module_id: str,
    review_round: int,
) -> lifecycle._CrossOwnerLaneResult:
    target = next(iter(REPORT_TAXONOMY[module_id].submodules))
    current = _module(module_id)
    revised = current.model_copy(
        update={
            "revision": 1,
            "submodule_narratives": {
                **current.submodule_narratives,
                target: current.submodule_narratives[target] + "\n\n已补充联合验收。",
            },
            "revision_responses": [
                RevisionResponse(
                    finding_id=f"X-{module_id}",
                    action="implemented",
                    summary="已补充责任接口、实施顺序和联合验收记录，并保留原有风险边界。",
                    changed_target_ids=[target],
                )
            ],
        }
    )
    subject_ref = f"Work/runs/{run_id}/modules/{module_id}-r1.json"
    local_ref = (
        f"Work/runs/{run_id}/reviews/module/cross-r{review_round}/"
        f"{module_id}/completion-r1.json"
    )
    validation_ref = f"Work/runs/{run_id}/validations/cross-{module_id}-r1.json"
    runner.service.store.write_json(subject_ref, revised.model_dump(mode="json"))
    runner.service.store.write_json(local_ref, {"status": "completed", "module_id": module_id})
    runner.service.store.write_json(
        validation_ref,
        ValidationReport(
            validation_protocol_version=2,
            run_id=run_id,
            subject_ref=subject_ref,
            subject_revision=1,
            content_sha256=hashlib.sha256(
                (runner.service.workspace / subject_ref).read_bytes()
            ).hexdigest(),
            validator="fake-cross-owner/v2",
            check_ids=["cross.owner"],
            passed=True,
        ).model_dump(mode="json"),
    )

    def artifact_ref(ref: str) -> ArtifactRef:
        content = (runner.service.workspace / ref).read_bytes()
        return ArtifactRef(
            ref=ref,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            media_type="application/json",
        )

    completion = CrossOwnerCompletion(
        lane_id=f"cross-r{review_round}-module-{module_id}",
        run_id=run_id,
        review_round=review_round,
        module_id=module_id,
        semantic_key=hashlib.sha256(module_id.encode()).hexdigest(),
        subject_revision=1,
        subject=artifact_ref(subject_ref),
        local_review_completion=artifact_ref(local_ref),
        machine_validation=artifact_ref(validation_ref),
        author_task_attempt_id=f"attempt-{module_id}",
        reviewer_session_id=f"module-auditor-{module_id}",
        lease_epoch=1,
    )
    completion_ref = (
        f"Work/runs/{run_id}/lanes/cross-r{review_round}/"
        f"module-{module_id}/completion-r1.json"
    )
    runner.service.store.write_json(completion_ref, completion.model_dump(mode="json"))
    return lifecycle._CrossOwnerLaneResult(
        module=revised,
        responses=revised.revision_responses,
        local_review_ref=local_ref,
        machine_validation_ref=validation_ref,
        completion_ref=completion_ref,
        completion=completion,
    )


@pytest.mark.asyncio
async def test_all_ready_cross_owner_lanes_use_distinct_owner_full_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-all-ready"
    owner_ids = ("2.1", "2.2", "2.3")
    finding_submission, verdict_submission, _findings = _cross_inputs(run_id, owner_ids)
    runner = _Runner(tmp_path, [finding_submission, verdict_submission])
    for module_id in REPORT_TAXONOMY:
        runner.service.store.write_json(
            f"Work/runs/{run_id}/modules/{module_id}-r0.json",
            _module(module_id).model_dump(mode="json"),
        )
    active = 0
    maximum_active = 0

    async def fake_lane(_runner, *, module_id, review_round, **_kwargs):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        result = _write_completion(
            runner,
            run_id=run_id,
            module_id=module_id,
            review_round=review_round,
        )
        active -= 1
        return result

    monkeypatch.setattr(lifecycle, "_run_cross_owner_lane", fake_lane)
    state = {
        "run_id": run_id,
        "request": SimpleNamespace(execution_mode="all_ready", module_lane_concurrency=1),
        "module_submissions": {module_id: _module(module_id) for module_id in REPORT_TAXONOMY},
        "module_review_completion_refs": {},
    }
    await lifecycle.run_cross_review(runner, state, "workflow-cross-all-ready")
    assert maximum_active == len(owner_ids)
    assert [agent for agent, _session in runner.calls] == [
        "cross-module-reviewer",
        "cross-module-reviewer",
    ]
    barrier = json.loads(
        (tmp_path / state["cross_owner_barrier_ref"]).read_text(encoding="utf-8")
    )
    assert barrier["target_modules"] == list(owner_ids)
    assert barrier["completion_revisions"] == {module_id: 1 for module_id in owner_ids}


@pytest.mark.asyncio
async def test_cross_owner_failure_drains_started_lanes_and_blocks_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-failure"
    owner_ids = ("2.1", "2.2", "2.3")
    finding_submission, _verdict_submission, findings = _cross_inputs(run_id, owner_ids)
    runner = _Runner(tmp_path, [finding_submission])
    for module_id in REPORT_TAXONOMY:
        runner.service.store.write_json(
            f"Work/runs/{run_id}/modules/{module_id}-r0.json",
            _module(module_id).model_dump(mode="json"),
        )
    started: set[str] = set()
    ready = asyncio.Event()

    async def fake_lane(_runner, *, module_id, review_round, **_kwargs):
        started.add(module_id)
        if started == set(owner_ids):
            ready.set()
        await ready.wait()
        if module_id == "2.2":
            raise RuntimeError("injected owner failure")
        return _write_completion(
            runner,
            run_id=run_id,
            module_id=module_id,
            review_round=review_round,
        )

    monkeypatch.setattr(lifecycle, "_run_cross_owner_lane", fake_lane)
    state = {
        "run_id": run_id,
        "request": SimpleNamespace(execution_mode="all_ready", module_lane_concurrency=1),
        "module_submissions": {module_id: _module(module_id) for module_id in REPORT_MODULE_IDS},
        "module_review_completion_refs": {},
    }
    with pytest.raises(RuntimeError, match="injected owner failure"):
        await lifecycle.run_cross_review(runner, state, "workflow-cross-failure")
    assert started == set(owner_ids)
    assert len(runner.calls) == 1  # Cross r1 cannot start after a failed owner.
    terminal_ref = tmp_path / f"Work/runs/{run_id}/lanes/cross-r0/owner-terminal.json"
    terminal = json.loads(terminal_ref.read_text(encoding="utf-8"))
    assert terminal["status"] == "failed"
    assert terminal["terminal_statuses"] == {
        "2.1": "completed",
        "2.2": "failed",
        "2.3": "completed",
    }
    assert not (
        tmp_path / f"Work/runs/{run_id}/lanes/cross-r0/owner-barrier.json"
    ).exists()
    progress = json.loads(
        (tmp_path / f"Work/runs/{run_id}/reviews/cross-progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(progress["revised_owner_ids"]) == {"2.1", "2.3"}


@pytest.mark.asyncio
async def test_resume_reuses_verified_owner_completion_and_does_not_replay_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-cross-resume"
    owner_ids = ("2.1", "2.2", "2.3")
    finding_submission, verdict_submission, _findings = _cross_inputs(run_id, owner_ids)
    runner = _Runner(tmp_path, [finding_submission])
    for module_id in REPORT_TAXONOMY:
        runner.service.store.write_json(
            f"Work/runs/{run_id}/modules/{module_id}-r0.json",
            _module(module_id).model_dump(mode="json"),
        )
    first_pass = True
    lane_calls: list[str] = []

    class _Unknown(RuntimeError):
        attempt_disposition = "accepted_or_unknown"

    async def flaky_lane(_runner, *, module_id, review_round, **_kwargs):
        nonlocal first_pass
        lane_calls.append(module_id)
        if first_pass and module_id == "2.2":
            raise RuntimeError("ordinary owner failure")
        return _write_completion(
            runner,
            run_id=run_id,
            module_id=module_id,
            review_round=review_round,
        )

    monkeypatch.setattr(lifecycle, "_run_cross_owner_lane", flaky_lane)
    state = {
        "run_id": run_id,
        "request": SimpleNamespace(execution_mode="all_ready", module_lane_concurrency=1),
        "module_submissions": {module_id: _module(module_id) for module_id in REPORT_MODULE_IDS},
        "module_review_completion_refs": {},
    }
    with pytest.raises(RuntimeError, match="ordinary owner failure"):
        await lifecycle.run_cross_review(runner, state, "workflow-cross-resume")
    assert lane_calls == ["2.1", "2.2", "2.3"]

    # A resumed wave only invokes the missing owner; the two verified
    # completions are promoted directly from their immutable artifacts.
    first_pass = False
    (tmp_path / f"Work/runs/{run_id}/reviews/cross-progress.json").unlink()
    runner.scripted.append(verdict_submission)
    state["resume"] = True
    await lifecycle.run_cross_review(runner, state, "workflow-cross-resume")
    assert lane_calls == ["2.1", "2.2", "2.3", "2.2"]
    assert [agent for agent, _session in runner.calls] == [
        "cross-module-reviewer",
        "cross-module-reviewer",
    ]

    # An accepted_or_unknown owner is not replayed even when the typed owner
    # completion is missing; the resume remains fail-closed before Cross r1.
    run_unknown = "run-cross-unknown"
    unknown_runner = _Runner(tmp_path / "unknown", [finding_submission])
    for module_id in REPORT_TAXONOMY:
        unknown_runner.service.store.write_json(
            f"Work/runs/{run_unknown}/modules/{module_id}-r0.json",
            _module(module_id).model_dump(mode="json"),
        )
    unknown_calls: list[str] = []

    async def unknown_lane(_runner, *, module_id, review_round, **_kwargs):
        unknown_calls.append(module_id)
        if module_id == "2.2":
            raise _Unknown("provider accepted request but typed result is unknown")
        return _write_completion(
            unknown_runner,
            run_id=run_unknown,
            module_id=module_id,
            review_round=review_round,
        )

    monkeypatch.setattr(lifecycle, "_run_cross_owner_lane", unknown_lane)
    unknown_state = {
        "run_id": run_unknown,
        "request": SimpleNamespace(execution_mode="all_ready", module_lane_concurrency=1),
        "module_submissions": {module_id: _module(module_id) for module_id in REPORT_MODULE_IDS},
        "module_review_completion_refs": {},
    }
    with pytest.raises(RuntimeError, match="accepted request"):
        await lifecycle.run_cross_review(unknown_runner, unknown_state, "workflow-cross-unknown")
    unknown_state["resume"] = True
    with pytest.raises(lifecycle.ReviewLifecycleError, match="accepted_or_unknown"):
        await lifecycle.run_cross_review(unknown_runner, unknown_state, "workflow-cross-unknown")
    assert unknown_calls.count("2.2") == 1


# Keep this local alias so the failure test remains readable without importing
# the workflow module's private constant into the fixture body.
REPORT_MODULE_IDS = tuple(REPORT_TAXONOMY)
