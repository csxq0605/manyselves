from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from manyselves.core.reporting.agentic_models import (
    AgentResult,
    AgentRunStatus,
    TaskEnvelope,
)
from manyselves.core.reporting.module_collaboration import (
    MODULE_IDS,
    InterfaceDisposition,
    InterfaceRequest,
    ModuleDiscoverySubmission,
    ModuleInterfaceCoverage,
    ModuleInterfaceResponseSubmission,
)
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.workflow import (
    AgentWorkflowError,
    ReportWorkflowRunner,
)


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

    await runner._module_collaboration(
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
    await runner._module_collaboration(
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
        await runner._module_collaboration(
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

    await runner._module_collaboration(
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
    await runner._module_collaboration(
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
