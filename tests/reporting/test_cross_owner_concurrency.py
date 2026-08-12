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
    CrossReviewCoverageEntry,
    CrossSynthesisInput,
    ModuleSubmission,
)
from manyselves.core.reporting.parallel_runtime import (
    ArtifactRef,
    CrossOwnerCompletion,
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


def _synthesis() -> list[CrossSynthesisInput]:
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
        CrossSynthesisInput(id="SI-RISK", cluster_type="risk_cluster", **shared),
        CrossSynthesisInput(id="SI-GLOBAL", cluster_type="global_propagation", **shared),
    ]


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
            synthesis_inputs=_synthesis(),
            interface_closures=[],
        )

    @staticmethod
    def _role_skill_context(_state, _role, **_kwargs):
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
    machine_ref = f"Work/runs/{run_id}/validations/cross-{owner_module_id}-r0.json"
    runner.service.store.write_json(local_ref, {"status": "completed", "module_id": owner_module_id})
    runner.service.store.write_json(machine_ref, {"status": "passed", "module_id": owner_module_id})
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
        machine_validation=_artifact_ref(runner, machine_ref),
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
        machine_validation_ref=machine_ref,
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


@pytest.mark.asyncio
async def test_cross_owner_initial_failure_drains_all_five_and_writes_no_barrier(
    tmp_path: Path,
) -> None:
    run_id = "run-cross-five-failure"
    runner = _Runner(tmp_path, failed_owner="2.3")
    _write_modules(runner, run_id)
    state = _state(run_id)

    with pytest.raises(RuntimeError, match="injected owner failure: 2.3"):
        await lifecycle.run_cross_review(runner, state, "workflow-cross-five-failure")

    owner_ids = tuple(REPORT_TAXONOMY)
    assert {owner for owner, _session in runner.calls} == set(owner_ids)
    terminal_path = (
        tmp_path / f"Work/runs/{run_id}/lanes/cross-r0/owner-terminal.json"
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
    assert not (
        tmp_path / f"Work/runs/{run_id}/lanes/cross-r0/owner-barrier.json"
    ).exists()
