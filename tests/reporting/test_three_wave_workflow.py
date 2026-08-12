from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from manyselves.core.reporting.agentic_models import (
    AgentResult,
    AgentRunStatus,
    ClaimRecord,
    ModuleSubmission,
    ModuleRevisionSubmission,
    SubmoduleDraftSubmission,
    TaskEnvelope,
)
from manyselves.core.reporting.input_contracts import RequestedModuleChange
from manyselves.core.reporting.distributed_runtime import LocalEventStore, RunProjection
from manyselves.core.reporting.module_collaboration import (
    MODULE_IDS,
    InterfaceDisposition,
    InterfaceRequest,
    ModuleDiscoverySubmission,
    ModuleInterfaceCoverage,
    ModuleInterfaceResponseSubmission,
    SubmoduleDiscoveryBatchSubmission,
    SubmoduleDiscoverySubmission,
    SubmoduleInterfaceResponseSubmission,
    SubmoduleInterfaceSignal,
)
from manyselves.core.reporting.models import ReportRequest
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY, resolve_submodule
from manyselves.core.reporting.workflow import (
    AgentWorkflowError,
    ReportWorkflowRunner,
)
from manyselves.core.reporting.review_lifecycle import (
    DeferredMainDecision,
    request_module_revision,
)
from manyselves.core.reporting.scheduling import TaskTimingHistory
from manyselves.core.reporting.source_ledger import SourceLedger


class _Service:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.store = ReportingStore(workspace)

    async def _notice(self, _message: str) -> None:
        return None


def _discovery(module_id: str) -> ModuleDiscoverySubmission:
    requests = (
        [
            InterfaceRequest(
                request_id="IF-2.1-2.3-001",
                requester_module_id="2.1",
                target_module_id="2.3",
                question="保护边界是否覆盖当前切换场景？",
                needed_for="确定架构风险和联合验收边界。",
                evidence_ids=["E-0001"],
            )
        ]
        if module_id == "2.1"
        else []
    )
    return ModuleDiscoverySubmission(
        module_id=module_id,
        discovery_summary=f"{module_id} 已完成一次可复用的证据发现。",
        evidence_ids=["E-0001"],
        interface_coverage=[
            ModuleInterfaceCoverage(
                target_module_id=peer,
                status=(
                    "request"
                    if module_id == "2.1" and peer == "2.3"
                    else "not_applicable"
                ),
                rationale=(
                    "需要保护责任模块确认接口边界。"
                    if module_id == "2.1" and peer == "2.3"
                    else "当前证据没有形成该接口依赖。"
                ),
            )
            for peer in MODULE_IDS
            if peer != module_id
        ],
        requests=requests,
    )


def _state(run_id: str) -> dict:
    preparation_refs = {
        "coverage": f"Work/runs/{run_id}/coverage.json",
        "evidence": f"Work/runs/{run_id}/evidence.jsonl",
        "manifest": f"Work/runs/{run_id}/manifest.json",
    }
    tasks = [
        TaskEnvelope(
            task_id=f"module-{module_id}",
            run_id=run_id,
            agent_id=f"module-{module_id}-specialist",
            objective=f"完成模块 {module_id}",
            input_refs=[
                *preparation_refs.values(),
                f"Work/runs/{run_id}/knowledge/module-{module_id}.md",
            ],
            artifact_delivery_modes={
                **{
                    ref: "reference"
                    for ref in preparation_refs.values()
                },
                (
                    f"Work/runs/{run_id}/knowledge/"
                    f"module-{module_id}.md"
                ): "reference",
            },
            target_submodule_ids=list(
                REPORT_TAXONOMY[module_id].submodules
            ),
        )
        for module_id in MODULE_IDS
    ]
    return {
        "run_id": run_id,
        "request": SimpleNamespace(
            missing_evidence_policy="draft",
            execution_requirements=[],
            user_supplements=[],
            submodule_task_concurrency=8,
            submodule_batch_size=14,
        ),
        "module_dispatch": SimpleNamespace(module_tasks=tasks),
        "module_knowledge_refs": {
            module_id: (
                f"Work/runs/{run_id}/knowledge/module-{module_id}.md"
            )
            for module_id in MODULE_IDS
        },
        "preparation_refs": preparation_refs,
        "evidence_items": [SimpleNamespace(id="E-0001")],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_mode", "expected_calls", "expected_selection"),
    [
        (
            "current_serial_review",
            ["leaf_collaboration", "leaf_authoring"],
            (True, False, True),
        ),
        (
            "bounded_module_lanes",
            [
                "leaf_collaboration",
                "leaf_authoring",
                "bounded_module_lanes:5",
            ],
            (True, True, True),
        ),
    ],
)
async def test_execution_mode_gates_leaf_authoring_and_module_lane_expansion(
    tmp_path: Path,
    execution_mode: str,
    expected_calls: list[str],
    expected_selection: tuple[bool, bool, bool],
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    calls: list[str] = []

    async def leaf(*_args, **_kwargs):
        calls.append("leaf_collaboration")

    async def local_leaf(*_args, **_kwargs):
        calls.append("local_leaf_preparation")

    async def leaf_authoring(*_args, **_kwargs):
        calls.append("leaf_authoring")

    async def lanes(*_args, concurrency: int, **_kwargs):
        calls.append(f"bounded_module_lanes:{concurrency}")

    runner._module_collaboration = leaf
    runner._module_local_submodule_preparation = local_leaf
    runner._run_submodule_authoring_stage = leaf_authoring
    runner._run_bounded_module_lanes = lanes
    request = ReportRequest(
        operation="full_report",
        instruction="验证执行模式边界",
        # Keep the historical serial mode explicit; omitted values now mean
        # the all-ready business path for new requests.
        execution_mode=execution_mode,
    )

    selected = await runner._prepare_module_authoring_mode(
        tuple(request.target_modules),
        {"request": request},
        "workflow-mode-gate",
    )

    assert selected == expected_selection
    assert calls == expected_calls


@pytest.mark.asyncio
async def test_module_five_runs_complete_module_lanes_without_leaf_waves(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    calls: list[tuple[str, int | bool]] = []

    async def unexpected_leaf(*_args, **_kwargs):
        raise AssertionError("module_5 must not start leaf collaboration or authoring")

    async def module_lanes(*_args, concurrency: int, all_ready: bool, **_kwargs):
        calls.append(("module_lanes", concurrency))
        calls.append(("all_ready", all_ready))

    runner._module_collaboration = unexpected_leaf
    runner._module_local_submodule_preparation = unexpected_leaf
    runner._run_submodule_authoring_stage = unexpected_leaf
    runner._run_bounded_module_lanes = module_lanes
    request = ReportRequest(
        operation="full_report",
        instruction="验证五模块完整 lane",
        authoring_granularity="module_5",
        # The authoring switch alone must force five complete all-ready lanes;
        # a legacy review mode cannot serialize this A/B arm.
        execution_mode="current_serial_review",
        module_lane_concurrency=1,
    )

    selected = await runner._prepare_module_authoring_mode(
        tuple(request.target_modules),
        {"request": request},
        "workflow-module-five",
    )

    assert selected == (False, True, True)
    assert calls == [("module_lanes", 1), ("all_ready", True)]


@pytest.mark.asyncio
async def test_submodule_three_wave_dispatches_independent_wave_one_and_two_leaves(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    runner._budget = None
    checkpoints: list[tuple[str, str]] = []
    runner._checkpoint = (
        lambda _state, activity, status, error=None: checkpoints.append(
            (activity, status)
        )
    )
    runner._cost_boundary = lambda *_args, **_kwargs: asyncio.sleep(0)
    envelopes: list[TaskEnvelope] = []
    response_active = 0
    response_max_active = 0

    async def agent(
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        envelopes.append(envelope)
        module_id = REPORT_TAXONOMY[
            envelope.agent_id.removeprefix("module-").removesuffix("-specialist")
        ].id
        if envelope.allowed_outputs == ["submodule_discovery_submission"]:
            submodule_id = envelope.target_submodule_ids[0]
            return SubmoduleDiscoverySubmission(
                module_id=module_id,
                submodule_id=submodule_id,
                discovery_summary=f"{submodule_id} 独立发现。",
                evidence_ids=["E-0001"],
                interface_signals=(
                    [
                        SubmoduleInterfaceSignal(
                            target_module_id="2.3",
                            target_submodule_id=(
                                "2.3.1" if submodule_id == "2.1.1" else "2.3.2"
                            ),
                            status="request",
                            rationale="需要保护边界。",
                            question=f"{submodule_id} 整定是否覆盖异常负荷边界？",
                            needed_for=f"完成{submodule_id}结论。",
                            evidence_ids=["E-0001"],
                        )
                    ]
                    if submodule_id in {"2.1.1", "2.1.2"}
                    else []
                ),
            )
        assert envelope.allowed_outputs == ["submodule_interface_response_submission"]
        assert envelope.agent_id == "module-2.3-specialist"
        nonlocal response_active, response_max_active
        response_active += 1
        response_max_active = max(response_max_active, response_active)
        await asyncio.sleep(0.002)
        response_active -= 1
        target_submodule_id = envelope.target_submodule_ids[0]
        source_submodule_id = {
            "2.3.1": "2.1.1",
            "2.3.2": "2.1.2",
        }[target_submodule_id]
        request_sequence = {"2.3.1": "001", "2.3.2": "002"}[target_submodule_id]
        return SubmoduleInterfaceResponseSubmission(
            module_id="2.3",
            submodule_id=target_submodule_id,
            dispositions=[
                InterfaceDisposition(
                    request_id=(
                        f"IF-{source_submodule_id}-{target_submodule_id}-"
                        f"{request_sequence}"
                    ),
                    status="answered",
                    answer="当前整定覆盖正常边界。",
                    evidence_ids=["E-0001"],
                    conditions=["以当前整定版本为准"],
                )
            ],
        )

    runner._agent = agent
    state = _state("run-submodule-three-wave")
    await runner._module_collaboration(
        tuple(MODULE_IDS), state, "workflow-submodule-three-wave"
    )

    discoveries = [
        item
        for item in envelopes
        if item.allowed_outputs == ["submodule_discovery_submission"]
    ]
    responses = [
        item
        for item in envelopes
        if item.allowed_outputs == ["submodule_interface_response_submission"]
    ]
    assert len(discoveries) == 37
    assert all(len(item.target_submodule_ids) == 1 for item in discoveries)
    assert len({item.task_id for item in discoveries}) == 37
    assert len({item.task_attempt_id for item in discoveries}) == 37
    assert len({item.task_id for item in discoveries} & {
        item.task_id for item in responses
    }) == 0
    assert all(
        item.allowed_tools
        == [
            "open_artifact",
            "search_text",
            "calculate",
            "report_gap",
            "report_blocked",
            "submit_result",
        ]
        for item in discoveries
    )
    assert all(len(item.input_refs) == 4 for item in discoveries)
    assert all(
        item.artifact_delivery_modes[item.input_refs[0]] == "reference"
        and all(
            item.artifact_delivery_modes[ref] == "reference"
            for ref in item.input_refs[1:]
        )
        for item in discoveries
    )
    assert all(
        "<leaf_context_delta" in (item.inline_context or "")
        and '"E-0001"' in (item.inline_context or "")
        for item in discoveries
    )
    sibling_shared_refs = {
        item.input_refs[1]
        for item in discoveries
        if item.agent_id == "module-2.1-specialist"
    }
    assert len(sibling_shared_refs) == 1
    assert len(responses) == 2
    assert {item.agent_id for item in responses} == {"module-2.3-specialist"}
    assert len({item.task_id for item in responses}) == len(responses)
    assert len({item.task_attempt_id for item in responses}) == len(responses)
    assert response_max_active == len(responses) == 2
    assert set(state["submodule_discovery_barrier_refs"]) == set(MODULE_IDS)
    assert len(state["submodule_discovery_context_sha256"]) == 37
    assert len(state["submodule_response_context_sha256"]) == 2
    assert len(state["submodule_collaboration_bundle_refs"]) == 37
    assert set(state["collaboration_bundle_refs"]) == set(MODULE_IDS)
    assert checkpoints[-2:] == [
        ("collaboration-barrier-1", "completed"),
        ("collaboration-barrier-2", "completed"),
    ]

    async def no_repeat(*_args, **_kwargs):
        raise AssertionError("verified leaf completions must recover without calls")

    runner._agent = no_repeat
    await runner._module_collaboration(
        tuple(MODULE_IDS), state, "workflow-submodule-three-wave"
    )

    knowledge_path = tmp_path / state["module_knowledge_refs"]["2.1"]
    knowledge_path.parent.mkdir(parents=True, exist_ok=True)
    knowledge_path.write_text("2.1 当前知识发生变化。", encoding="utf-8")
    envelopes.clear()
    runner._agent = agent
    await runner._module_collaboration(
        tuple(MODULE_IDS), state, "workflow-submodule-three-wave"
    )
    assert {
        submodule_id
        for item in envelopes
        if item.allowed_outputs == ["submodule_discovery_submission"]
        for submodule_id in item.target_submodule_ids
    } == set(REPORT_TAXONOMY["2.1"].submodules)
    assert not [
        item
        for item in envelopes
        if item.allowed_outputs == ["submodule_interface_response_submission"]
    ]


@pytest.mark.asyncio
async def test_module_report_leaf_failure_drains_all_ready_siblings_and_resumes_failed_leaf(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    runner._budget = None
    runner._checkpoint = lambda *_args, **_kwargs: None
    runner._cost_boundary = lambda *_args, **_kwargs: asyncio.sleep(0)
    state = _state("run-module-local-leaf-resume")
    state["request"].submodule_task_concurrency = 1
    expected = tuple(REPORT_TAXONOMY["2.4"].submodules)
    failing_id = expected[4]
    first_started: list[tuple[str, ...]] = []
    first_succeeded: list[str] = []

    async def first_agent(
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        submodule_id = envelope.target_submodule_ids[0]
        first_started.append((submodule_id,))
        if submodule_id == failing_id:
            raise AgentWorkflowError("injected module-local leaf failure")
        first_succeeded.append(submodule_id)
        return SubmoduleDiscoverySubmission(
            module_id="2.4",
            submodule_id=submodule_id,
            discovery_summary=f"{submodule_id} 独立发现。",
            evidence_ids=["E-0001"],
            interface_signals=[],
        )

    runner._agent = first_agent
    with pytest.raises(AgentWorkflowError, match="injected module-local leaf failure"):
        await runner._module_local_submodule_preparation(
            ("2.4",), state, "workflow-module-local-leaf-resume"
        )
    assert len(first_started) == len(expected)
    assert set(first_succeeded) == set(expected) - {failing_id}

    resumed_calls: list[tuple[str, ...]] = []

    async def resumed_agent(
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        submodule_id = envelope.target_submodule_ids[0]
        resumed_calls.append((submodule_id,))
        return SubmoduleDiscoverySubmission(
            module_id="2.4",
            submodule_id=submodule_id,
            discovery_summary=f"{submodule_id} 恢复发现。",
            evidence_ids=["E-0001"],
            interface_signals=[],
        )

    runner._agent = resumed_agent
    await runner._module_local_submodule_preparation(
        ("2.4",), state, "workflow-module-local-leaf-resume"
    )

    resumed_leaf_ids = {item for batch in resumed_calls for item in batch}
    assert resumed_leaf_ids == set(expected) - set(first_succeeded)
    assert not resumed_leaf_ids.intersection(first_succeeded)
    assert set(state["submodule_discovery_barrier_refs"]) == {"2.4"}
    assert set(state["submodule_collaboration_bundle_refs"]) == set(expected)
    for ref in state["submodule_collaboration_bundle_refs"].values():
        bundle = json.loads((tmp_path / ref).read_text(encoding="utf-8"))
        assert bundle["requested_interfaces"] == []
        assert bundle["responded_interfaces"] == []


@pytest.mark.asyncio
async def test_leaf_revision_failure_keeps_completed_siblings_for_same_run_resume(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    runner._budget = None
    runner._role_skill_context = lambda *_args, **_kwargs: ""
    runner._user_supplement_constraints = lambda *_args, **_kwargs: []
    state = _state("run-leaf-revision-resume")
    state["request"].submodule_task_concurrency = 3
    state["request"].user_supplements = []
    targets = tuple(REPORT_TAXONOMY["2.4"].submodules)[:5]
    subject = ModuleSubmission(
        module_id="2.4",
        submodule_narratives={
            submodule_id: f"### {submodule_id}\n\n{submodule_id} 原始正文。"
            for submodule_id in REPORT_TAXONOMY["2.4"].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    runner.service.store.write_json(
        "Work/runs/run-leaf-revision-resume/modules/2.4-r0.json",
        subject.model_dump(mode="json"),
    )
    change = RequestedModuleChange(
        id="USER-2.4-R1",
        instruction="分别更新五个固定叶子。",
        target_submodule_ids=list(targets),
    )
    failing_id = targets[0]
    first_started: list[str] = []
    first_succeeded: list[str] = []
    cohort_started = asyncio.Event()

    def patch(submodule_id: str) -> ModuleRevisionSubmission:
        return ModuleRevisionSubmission(
            module_id="2.4",
            base_revision=0,
            revision=1,
            submodule_narratives={
                submodule_id: f"### {submodule_id}\n\n{submodule_id} 已独立修订。"
            },
            claims_upsert=[],
            claim_ids_remove=[],
            source_ids=[],
            unresolved_questions=[],
            revision_responses=[
                {
                    "finding_id": change.id,
                    "action": "implemented",
                    "summary": f"已由原叶子身份完成 {submodule_id} 的独立修订。",
                    "changed_target_ids": [submodule_id],
                }
            ],
        )

    async def first_agent(
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        submodule_id = envelope.target_submodule_ids[0]
        assert session_key == f"submodule-{submodule_id}"
        first_started.append(submodule_id)
        if len(first_started) == 3:
            cohort_started.set()
        await cohort_started.wait()
        if submodule_id == failing_id:
            raise AgentWorkflowError("injected leaf revision failure")
        await asyncio.sleep(0.002)
        first_succeeded.append(submodule_id)
        return patch(submodule_id)

    runner._agent = first_agent
    with pytest.raises(AgentWorkflowError, match="injected leaf revision failure"):
        await request_module_revision(
            runner,
            state=state,
            workflow_id="workflow-leaf-revision-resume",
            subject=subject,
            requested_changes=[change],
        )
    # all_ready admits every affected leaf; the old fixed worker-cap assertion
    # (three) no longer describes this revision cohort.
    assert len(first_started) == len(targets) == 5
    assert set(first_succeeded) == set(first_started) - {failing_id}

    resumed_calls: list[str] = []

    async def resumed_agent(
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        submodule_id = envelope.target_submodule_ids[0]
        assert session_key == f"submodule-{submodule_id}"
        resumed_calls.append(submodule_id)
        return patch(submodule_id)

    runner._agent = resumed_agent
    revised, _ = await request_module_revision(
        runner,
        state=state,
        workflow_id="workflow-leaf-revision-resume",
        subject=subject,
        requested_changes=[change],
    )

    assert set(resumed_calls) == set(targets) - set(first_succeeded)
    assert not set(resumed_calls).intersection(first_succeeded)
    assert revised.revision == 1
    assert revised.revision_responses[0].changed_target_ids == sorted(targets)
    barrier = json.loads(
        (
            tmp_path
            / "Work/runs/run-leaf-revision-resume/reviews/module-revisions/"
            "2.4/r1/reducer-barrier.json"
        ).read_text(encoding="utf-8")
    )
    assert barrier["target_submodule_ids"] == sorted(targets)
    assert set(barrier["patch_sha256"]) == set(targets)


@pytest.mark.asyncio
async def test_wave_three_dispatches_and_persists_37_independent_leaf_results(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    runner._budget = None
    runner._checkpoint = lambda *_args, **_kwargs: None
    runner._cost_boundary = lambda *_args, **_kwargs: asyncio.sleep(0)
    state = _state("run-submodule-wave-three")
    for module_id, knowledge_ref in state["module_knowledge_refs"].items():
        runner.service.store.write_text(
            knowledge_ref,
            "# shared header\n\n"
            + "\n\n".join(
                f"## {leaf_id} {leaf.title}\nKNOWLEDGE-{leaf_id}"
                for leaf_id, leaf in REPORT_TAXONOMY[module_id].submodules.items()
            ),
        )
    state["template_skill_text"] = {
        "core": "CORE-METHOD-SENTINEL",
        "analysis": "FULL-ANALYSIS-SENTINEL",
        "visual": "FULL-VISUAL-SENTINEL",
        "rubric": "FULL-RUBRIC-SENTINEL",
    }
    state["template_skill_refs"] = {}
    for key, text in state["template_skill_text"].items():
        ref = f"Work/runs/{state['run_id']}/template-skill/{key}.md"
        runner.service.store.write_text(ref, text)
        state["template_skill_refs"][key] = ref

    async def discovery_agent(
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        assert envelope.allowed_outputs == ["submodule_discovery_submission"]
        module_id = REPORT_TAXONOMY[
            envelope.agent_id.removeprefix("module-").removesuffix("-specialist")
        ].id
        submodule_id = envelope.target_submodule_ids[0]
        return SubmoduleDiscoverySubmission(
            module_id=module_id,
            submodule_id=submodule_id,
            discovery_summary=f"{submodule_id} 独立发现。",
            evidence_ids=["E-0001"],
            interface_signals=[],
        )

    runner._agent = discovery_agent
    await runner._module_collaboration(
        tuple(MODULE_IDS), state, "workflow-submodule-wave-three"
    )
    SourceLedger(tmp_path, state["run_id"]).register_project(
        "E-0001",
        "测试证据",
        "Inputs/evidence.txt",
        "当前项目测试证据。",
    )

    active = 0
    max_active = 0
    calls: list[str] = []
    author_envelopes: list[TaskEnvelope] = []

    async def author(
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        nonlocal active, max_active
        assert envelope.allowed_outputs == ["submodule_draft_submission"]
        assert len(envelope.target_submodule_ids) == 1
        submodule_id = envelope.target_submodule_ids[0]
        module_id = resolve_submodule(submodule_id).module_id
        assert session_key == f"submodule-{submodule_id}"
        assert set(envelope.artifact_delivery_modes) == set(envelope.input_refs)
        author_envelopes.append(envelope)
        calls.append(submodule_id)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.002)
        active -= 1
        claim = ClaimRecord(
            id=f"C-{submodule_id}",
            module_id=module_id,
            submodule_id=submodule_id,
            text=f"{submodule_id} 已完成独立判断。",
            claim_type="project_fact",
            source_ids=["E-0001"],
        )
        return SubmoduleDraftSubmission(
            module_id=module_id,
            submodule_id=submodule_id,
            narrative=f"{submodule_id} 正文。[[CLAIM:C-{submodule_id}]]",
            claim=claim,
            source_ids=["E-0001"],
            unresolved_questions=[],
            revision=0,
        )

    runner._agent = author
    await runner._run_submodule_authoring_stage(
        tuple(MODULE_IDS), state, "workflow-submodule-wave-three"
    )

    expected_leaves = {
        submodule_id
        for module_id in MODULE_IDS
        for submodule_id in REPORT_TAXONOMY[module_id].submodules
    }
    assert len(calls) == 37
    assert set(calls) == expected_leaves
    assert max_active == len(expected_leaves)
    assert all("CORE-METHOD-SENTINEL" in item.inline_context for item in author_envelopes)
    assert all(
        "FULL-ANALYSIS-SENTINEL" not in item.inline_context
        and "FULL-VISUAL-SENTINEL" not in item.inline_context
        and "FULL-RUBRIC-SENTINEL" not in item.inline_context
        for item in author_envelopes
    )
    for item in author_envelopes:
        leaf_id = item.target_submodule_ids[0]
        assert f"KNOWLEDGE-{leaf_id}" in item.inline_context
        sibling_shared = [
            ref
            for ref in item.input_refs
            if "/context/shared/module-author-module-" in ref
        ]
        assert len(sibling_shared) == 1
    for module_id in MODULE_IDS:
        shared_refs = {
            next(
                ref
                for ref in item.input_refs
                if "/context/shared/module-author-module-" in ref
            )
            for item in author_envelopes
            if item.agent_id == f"module-{module_id}-specialist"
        }
        assert len(shared_refs) == 1
    assert set(state["submodule_authoring_barrier_refs"]) == set(MODULE_IDS)
    assert set(state["specialist_submissions"]) == set(MODULE_IDS)
    for module_id, submission in state["specialist_submissions"].items():
        assert set(submission.submodule_narratives) == set(
            REPORT_TAXONOMY[module_id].submodules
        )
        assert len(submission.claims) == len(
            REPORT_TAXONOMY[module_id].submodules
        )
        assert runner._module_authoring_completion_is_current(
            state, module_id, submission
        )

    async def no_repeat(*_args, **_kwargs):
        raise AssertionError("current leaf author completion must be reused")

    runner._agent = no_repeat
    await runner._run_submodule_authoring_stage(
        tuple(MODULE_IDS), state, "workflow-submodule-wave-three"
    )


@pytest.mark.asyncio
async def test_leaf_scheduler_freezes_new_dispatch_and_keeps_started_successes(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    submodule_ids = tuple(REPORT_TAXONOMY["2.4"].submodules)[:8]
    started: list[str] = []
    persisted: list[str] = []
    cohort_started = asyncio.Event()
    failing_id = submodule_ids[0]

    async def execute(submodule_id: str) -> str:
        started.append(submodule_id)
        if len(started) == 4:
            cohort_started.set()
        await cohort_started.wait()
        if submodule_id == failing_id:
            raise AgentWorkflowError("leaf failed")
        await asyncio.sleep(0.002)
        return submodule_id

    with pytest.raises(AgentWorkflowError, match="leaf failed"):
        await runner._run_scheduled_submodule_stage(
            submodule_ids,
            run_id="run-leaf-scheduler-failure",
            workflow_id="workflow-leaf-scheduler-failure",
            task_kind="submodule_authoring",
            concurrency=4,
            execute=execute,
            persist=lambda submodule_id, _payload: persisted.append(submodule_id),
        )

    assert len(started) == 4
    assert set(persisted) == set(started) - {failing_id}
    event_types = [
        event.event_type
        for event in LocalEventStore(
            tmp_path, "run-leaf-scheduler-failure"
        ).read()
    ]
    assert event_types.count("TaskDispatched") == 4
    assert event_types.count("TypedResultAccepted") == 3
    assert event_types.count("TaskFailed") == 1
    assert "StageCompleted" not in event_types


@pytest.mark.asyncio
async def test_three_wave_barriers_use_sparse_wave_two_and_resume_without_calls(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    runner._budget = None
    checkpoints: list[tuple[str, str]] = []
    boundaries: list[tuple[str, str | None]] = []
    runner._checkpoint = (
        lambda _state, activity, status, error=None: checkpoints.append(
            (activity, status)
        )
    )

    async def boundary(completed: str, next_stage: str | None) -> None:
        boundaries.append((completed, next_stage))

    runner._cost_boundary = boundary
    envelopes: list[TaskEnvelope] = []

    async def agent(
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        envelopes.append(envelope)
        module_id = envelope.agent_id.removeprefix("module-").removesuffix(
            "-specialist"
        )
        if envelope.allowed_outputs == ["module_discovery_submission"]:
            return _discovery(module_id)
        assert envelope.allowed_outputs == [
            "module_interface_response_submission"
        ]
        assert module_id == "2.3"
        return ModuleInterfaceResponseSubmission(
            module_id="2.3",
            dispositions=[
                InterfaceDisposition(
                    request_id="IF-2.1-2.3-001",
                    status="answered",
                    answer="当前保护边界覆盖正常切换，异常切换仍需联合试验。",
                    evidence_ids=["E-0001"],
                    conditions=["以当前整定版本为准"],
                )
            ],
        )

    runner._agent = agent
    state = _state("run-three-wave")

    await runner._module_collaboration_legacy(
        tuple(MODULE_IDS),
        state,
        "workflow-three-wave",
    )

    discovery_envelopes = [
        item
        for item in envelopes
        if item.allowed_outputs == ["module_discovery_submission"]
    ]
    response_envelopes = [
        item
        for item in envelopes
        if item.allowed_outputs
        == ["module_interface_response_submission"]
    ]
    assert len(discovery_envelopes) == 5
    assert [item.agent_id for item in response_envelopes] == [
        "module-2.3-specialist"
    ]
    assert set(response_envelopes[0].artifact_delivery_modes) == set(
        response_envelopes[0].input_refs
    )
    assert (
        state["preparation_refs"]["coverage"]
        not in response_envelopes[0].artifact_delivery_modes
    )
    assert (
        state["preparation_refs"]["manifest"]
        not in response_envelopes[0].artifact_delivery_modes
    )
    assert all("query_peer" not in item.allowed_tools for item in envelopes)
    assert checkpoints == [
        ("collaboration-barrier-1", "completed"),
        ("collaboration-barrier-2", "completed"),
    ]
    assert boundaries == [
        (
            "collaboration-discovery",
            "collaboration-interface-response",
        ),
        ("collaboration-interface-response", "module-authoring"),
    ]
    assert set(state["collaboration_bundle_refs"]) == set(MODULE_IDS)
    for ref in state["collaboration_bundle_refs"].values():
        assert (tmp_path / ref).is_file()

    async def no_repeat(*_args, **_kwargs):
        raise AssertionError("completed collaboration task was repeated")

    runner._agent = no_repeat
    await runner._module_collaboration_legacy(
        tuple(MODULE_IDS),
        state,
        "workflow-three-wave",
    )


@pytest.mark.asyncio
async def test_barrier_invalid_generic_result_is_quarantined_for_same_run_retry(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    runner._budget = None
    runner._checkpoint = lambda *_args, **_kwargs: None

    async def boundary(*_args, **_kwargs) -> None:
        return None

    runner._cost_boundary = boundary
    state = _state("run-barrier-retry")
    stale = _discovery("2.1").model_copy(
        update={"evidence_ids": ["E-STALE"]}
    )
    runner.service.store.write_json(
        (
            "Work/runs/run-barrier-retry/results/"
            "module-discovery-2.1.json"
        ),
        AgentResult(
            task_id="module-discovery-2.1",
            run_id="run-barrier-retry",
            agent_id="module-2.1-specialist",
            session_id="stale-session",
            status=AgentRunStatus.COMPLETED,
            payload=stale,
        ).model_dump(mode="json"),
    )

    dispatched: list[tuple[str, str]] = []

    async def valid_agent(
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        module_id = envelope.agent_id.removeprefix("module-").removesuffix(
            "-specialist"
        )
        dispatched.append((envelope.allowed_outputs[0], module_id))
        if envelope.allowed_outputs == ["module_discovery_submission"]:
            return _discovery(module_id)
        return ModuleInterfaceResponseSubmission(
            module_id="2.3",
            dispositions=[
                InterfaceDisposition(
                    request_id="IF-2.1-2.3-001",
                    status="answered",
                    answer="当前保护边界覆盖正常切换。",
                    conditions=["以当前整定版本为准"],
                )
            ],
        )

    runner._agent = valid_agent
    with pytest.raises(AgentWorkflowError, match="Barrier 1 failed"):
        await runner._module_collaboration_legacy(
            tuple(MODULE_IDS),
            state,
            "workflow-barrier-retry",
        )

    assert not (
        tmp_path
        / "Work/runs/run-barrier-retry/results/module-discovery-2.1.json"
    ).exists()
    rejected = list(
        (
            tmp_path
            / "Work/runs/run-barrier-retry/collaboration/rejected/wave-1"
        ).glob("*/result/module-discovery-2.1.json")
    )
    assert len(rejected) == 1

    await runner._module_collaboration_legacy(
        tuple(MODULE_IDS),
        state,
        "workflow-barrier-retry",
    )
    assert set(state["collaboration_bundle_refs"]) == set(MODULE_IDS)
    discovery_dispatches = [
        module_id
        for output, module_id in dispatched
        if output == "module_discovery_submission"
    ]
    assert sorted(discovery_dispatches) == sorted(MODULE_IDS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity_field", "wrong_value"),
    (
        ("run_id", "run-from-another-execution"),
        ("task_id", "module-discovery-2.2"),
        ("agent_id", "module-2.2-specialist"),
    ),
)
async def test_barrier_quarantines_wrong_generic_result_identity_and_retries_now(
    tmp_path: Path,
    identity_field: str,
    wrong_value: str,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    runner._budget = None
    runner._checkpoint = lambda *_args, **_kwargs: None

    async def boundary(*_args, **_kwargs) -> None:
        return None

    runner._cost_boundary = boundary
    run_id = "run-generic-identity-retry"
    state = _state(run_id)
    invalid_summary = "该 generic result 身份错误，绝不能提升到 Barrier。"
    invalid_result = AgentResult(
        task_id="module-discovery-2.1",
        run_id=run_id,
        agent_id="module-2.1-specialist",
        session_id="wrong-identity-session",
        status=AgentRunStatus.COMPLETED,
        payload=_discovery("2.1").model_copy(
            update={"discovery_summary": invalid_summary}
        ),
    ).model_copy(update={identity_field: wrong_value})
    generic_result_ref = (
        f"Work/runs/{run_id}/results/module-discovery-2.1.json"
    )
    runner.service.store.write_json(
        generic_result_ref,
        invalid_result.model_dump(mode="json"),
    )

    dispatched: list[str] = []

    async def valid_agent(
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        module_id = envelope.agent_id.removeprefix("module-").removesuffix(
            "-specialist"
        )
        dispatched.append(envelope.task_id)
        if envelope.allowed_outputs == ["module_discovery_submission"]:
            return _discovery(module_id)
        return ModuleInterfaceResponseSubmission(
            module_id="2.3",
            dispositions=[
                InterfaceDisposition(
                    request_id="IF-2.1-2.3-001",
                    status="answered",
                    answer="当前保护边界覆盖正常切换。",
                    conditions=["以当前整定版本为准"],
                )
            ],
        )

    runner._agent = valid_agent
    await runner._module_collaboration_legacy(
        tuple(MODULE_IDS),
        state,
        "workflow-generic-identity-retry",
    )

    assert "module-discovery-2.1" in dispatched
    promoted_ref = (
        tmp_path
        / f"Work/runs/{run_id}/collaboration/wave-1/module-2.1.json"
    )
    promoted = ModuleDiscoverySubmission.model_validate_json(
        promoted_ref.read_text(encoding="utf-8")
    )
    assert promoted.discovery_summary != invalid_summary
    assert not (tmp_path / generic_result_ref).exists()
    quarantined = list(
        (
            tmp_path
            / f"Work/runs/{run_id}/collaboration/rejected/wave-1"
        ).glob("*/result/module-discovery-2.1.json")
    )
    assert len(quarantined) == 1
    rejected = AgentResult.model_validate_json(
        quarantined[0].read_text(encoding="utf-8")
    )
    assert getattr(rejected, identity_field) == wrong_value
    assert set(state["collaboration_bundle_refs"]) == set(MODULE_IDS)


def test_async_gather_contains_only_authoring_pipeline_without_review() -> None:
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(ReportWorkflowRunner.run))
    )
    gathered_pipelines: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "gather"
        ):
            continue
        gathered_pipelines.extend(
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "_module_pipeline"
        )
    assert gathered_pipelines
    for call in gathered_pipelines:
        review = next(
            (
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "review"
            ),
            None,
        )
        assert isinstance(review, ast.Constant)
        assert review.value is False


@pytest.mark.asyncio
async def test_bounded_module_lanes_overlap_and_reduce_private_state_once(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    runner._budget = None
    checkpoint_states: list[dict] = []
    runner._checkpoint = lambda state, *_args, **_kwargs: checkpoint_states.append(
        dict(state)
    )
    runner._bind_reviewed_module_to_authoring_context = (
        lambda *_args, **_kwargs: None
    )
    run_id = "run-bounded-lanes"
    preparation_refs = {
        "coverage": f"Work/runs/{run_id}/coverage.json",
        "evidence": f"Work/runs/{run_id}/evidence.json",
        "manifest": f"Work/runs/{run_id}/manifest.json",
    }
    for ref in preparation_refs.values():
        runner.service.store.write_json(ref, {"ref": ref})
    state = {
        "run_id": run_id,
        "request": SimpleNamespace(
            execution_requirements=[],
            missing_evidence_policy="draft",
            user_supplements=[],
        ),
        "preparation_refs": preparation_refs,
        "module_knowledge_refs": {},
        "module_submissions": {},
        "specialist_submissions": {},
        "module_review_completion_refs": {},
    }
    active = 0
    maximum_active = 0
    lane_state_ids: set[int] = set()
    lane_starts: list[str] = []
    TaskTimingHistory(tmp_path).record(
        run_id="historical-run",
        task_id="2.5",
        task_kind="module_lane",
        owner_key="2.5",
        duration_ms=120_000,
        status="completed",
    )

    async def pipeline(
        module_id,
        lane_state,
        _workflow_id,
        *,
        review=True,
        checkpoint=True,
    ):
        nonlocal active, maximum_active
        assert review is True
        assert checkpoint is False
        lane_starts.append(module_id)
        lane_state_ids.add(id(lane_state))
        lane_state["private_module"] = module_id
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        submission = ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                submodule_id: f"{submodule_id} 模块 {module_id} 正文"
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            },
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )
        subject_ref = f"Work/runs/{run_id}/modules/{module_id}-r0.json"
        review_ref = (
            f"Work/runs/{run_id}/reviews/module/initial/{module_id}/"
            "completion-r0.json"
        )
        runner.service.store.write_json(
            subject_ref, submission.model_dump(mode="json")
        )
        runner.service.store.write_json(
            review_ref,
            {"reviewer_session_key": f"module-auditor-{module_id}"},
        )
        lane_state.setdefault("module_submissions", {})[module_id] = submission
        lane_state.setdefault("specialist_submissions", {})[module_id] = submission
        lane_state.setdefault("module_review_completion_refs", {})[
            module_id
        ] = review_ref
        return submission

    runner._module_pipeline = pipeline
    await runner._run_bounded_module_lanes(
        tuple(MODULE_IDS),
        state,
        "workflow-bounded-lanes",
        concurrency=2,
    )

    assert maximum_active == 2
    assert lane_starts[0] == "2.5"
    assert len(lane_state_ids) == 5
    assert "private_module" not in state
    assert set(state["module_submissions"]) == set(MODULE_IDS)
    assert set(state["module_review_completion_refs"]) == set(MODULE_IDS)
    barrier_ref = state["module_lane_barrier_ref"]
    barrier = json.loads((tmp_path / barrier_ref).read_text(encoding="utf-8"))
    assert barrier["target_modules"] == list(MODULE_IDS)
    assert list(barrier["completion_refs"]) == list(MODULE_IDS)
    assert checkpoint_states[-1]["module_lane_barrier_ref"] == barrier_ref
    schedule = json.loads(
        (tmp_path / f"Work/runs/{run_id}/scheduling/module-lanes.json").read_text(
            encoding="utf-8"
        )
    )
    assert schedule["policy"] == "longest_critical_path_first_v1"
    assert schedule["decisions"][0]["task_id"] == "2.5"
    events = LocalEventStore(tmp_path, run_id).read()
    assert events[0].event_type == "StageReady"
    assert events[-1].event_type == "StageCompleted"
    assert sum(event.event_type == "TaskDispatched" for event in events) == 5
    assert sum(event.event_type == "AttemptStarted" for event in events) == 5
    assert sum(event.event_type == "TypedResultAccepted" for event in events) == 5
    projection = RunProjection.rebuild(run_id, events)
    assert projection.stages["module-work"]["status"] == "completed"
    assert all(
        projection.tasks[f"module-{module_id}"]["status"] == "completed"
        for module_id in MODULE_IDS
    )


def _bounded_lane_fixture(
    tmp_path: Path,
    run_id: str,
) -> tuple[ReportWorkflowRunner, dict]:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    runner._budget = None
    runner._checkpoint = lambda *_args, **_kwargs: None
    runner._bind_reviewed_module_to_authoring_context = (
        lambda *_args, **_kwargs: None
    )
    preparation_refs = {
        "coverage": f"Work/runs/{run_id}/coverage.json",
        "evidence": f"Work/runs/{run_id}/evidence.json",
        "manifest": f"Work/runs/{run_id}/manifest.json",
    }
    for ref in preparation_refs.values():
        runner.service.store.write_json(ref, {"ref": ref})
    state = {
        "run_id": run_id,
        "request": SimpleNamespace(
            execution_requirements=[],
            missing_evidence_policy="draft",
            user_supplements=[],
        ),
        "preparation_refs": preparation_refs,
        "module_knowledge_refs": {},
        "module_submissions": {},
        "specialist_submissions": {},
        "module_review_completion_refs": {},
    }
    return runner, state


def _persist_lane_submission(
    runner: ReportWorkflowRunner,
    lane_state: dict,
    module_id: str,
) -> ModuleSubmission:
    run_id = lane_state["run_id"]
    submission = ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"{submodule_id} 模块 {module_id} 正文"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    subject_ref = f"Work/runs/{run_id}/modules/{module_id}-r0.json"
    review_ref = (
        f"Work/runs/{run_id}/reviews/module/initial/{module_id}/"
        "completion-r0.json"
    )
    runner.service.store.write_json(subject_ref, submission.model_dump(mode="json"))
    runner.service.store.write_json(
        review_ref,
        {"reviewer_session_key": f"module-auditor-{module_id}"},
    )
    lane_state.setdefault("module_submissions", {})[module_id] = submission
    lane_state.setdefault("specialist_submissions", {})[module_id] = submission
    lane_state.setdefault("module_review_completion_refs", {})[module_id] = review_ref
    return submission


@pytest.mark.asyncio
async def test_hard_failure_freezes_new_lanes_but_drains_started_cohort(
    tmp_path: Path,
) -> None:
    runner, state = _bounded_lane_fixture(tmp_path, "run-lane-failure")
    started: list[str] = []
    finished: list[str] = []

    async def pipeline(module_id, lane_state, _workflow_id, **_kwargs):
        started.append(module_id)
        if module_id == "2.2":
            await asyncio.sleep(0.005)
            raise AgentWorkflowError("injected lane failure")
        await asyncio.sleep(0.03)
        finished.append(module_id)
        return _persist_lane_submission(runner, lane_state, module_id)

    runner._module_pipeline = pipeline
    with pytest.raises(AgentWorkflowError, match="injected lane failure"):
        await runner._run_bounded_module_lanes(
            tuple(MODULE_IDS),
            state,
            "workflow-lane-failure",
            concurrency=2,
        )

    assert started == ["2.1", "2.2"]
    assert finished == ["2.1"]
    assert not (tmp_path / "Work/runs/run-lane-failure/lanes/module-barrier.json").exists()
    candidates = list(
        (tmp_path / "Work/runs/run-lane-failure/lanes/module-2.2/exceptions").glob(
            "*.json"
        )
    )
    assert len(candidates) == 1
    candidate = json.loads(candidates[0].read_text(encoding="utf-8"))
    assert candidate["disposition"] == "failed"
    events = LocalEventStore(tmp_path, "run-lane-failure").read()
    assert sum(event.event_type == "TaskFailed" for event in events) == 1
    assert sum(event.event_type == "TypedResultAccepted" for event in events) == 1
    assert not any(event.event_type == "StageCompleted" for event in events)


@pytest.mark.asyncio
async def test_completion_before_barrier_recovers_without_agent_calls(
    tmp_path: Path,
) -> None:
    runner, state = _bounded_lane_fixture(tmp_path, "run-lane-recovery")

    async def pipeline(module_id, lane_state, _workflow_id, **_kwargs):
        return _persist_lane_submission(runner, lane_state, module_id)

    runner._module_pipeline = pipeline
    for module_id in MODULE_IDS:
        await runner._execute_module_lane(
            module_id,
            state,
            "workflow-lane-recovery",
        )

    calls = 0

    async def must_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("verified completion must recover without Agent call")

    runner._module_pipeline = must_not_run
    await runner._run_bounded_module_lanes(
        tuple(MODULE_IDS),
        state,
        "workflow-lane-recovery",
        concurrency=2,
    )

    assert calls == 0
    assert set(state["module_submissions"]) == set(MODULE_IDS)
    assert (tmp_path / state["module_lane_barrier_ref"]).is_file()
    stage_ready = [
        event
        for event in LocalEventStore(tmp_path, "run-lane-recovery").read()
        if event.event_type == "StageReady"
    ]
    assert stage_ready[-1].payload["recovered_modules"] == list(MODULE_IDS)


@pytest.mark.asyncio
async def test_main_exception_work_starts_only_after_module_cohort_drains(
    tmp_path: Path,
) -> None:
    runner, state = _bounded_lane_fixture(tmp_path, "run-main-deferred")
    initial_started: set[str] = set()
    initial_finished: set[str] = set()
    active = 0
    main_boundary_snapshots: list[tuple[set[str], int]] = []

    async def pipeline(module_id, lane_state, _workflow_id, **_kwargs):
        nonlocal active
        if lane_state["_defer_main_exceptions"]:
            initial_started.add(module_id)
            active += 1
            await asyncio.sleep(0.005)
            active -= 1
            if module_id == "2.2":
                raise DeferredMainDecision("needs serialized Main decision")
            initial_finished.add(module_id)
        else:
            main_boundary_snapshots.append((set(initial_started), active))
        return _persist_lane_submission(runner, lane_state, module_id)

    runner._module_pipeline = pipeline
    await runner._run_bounded_module_lanes(
        tuple(MODULE_IDS),
        state,
        "workflow-main-deferred",
        concurrency=2,
    )

    assert initial_started == set(MODULE_IDS)
    assert initial_finished == set(MODULE_IDS) - {"2.2"}
    assert main_boundary_snapshots == [(set(MODULE_IDS), 0)]
    assert (tmp_path / state["module_lane_barrier_ref"]).is_file()
