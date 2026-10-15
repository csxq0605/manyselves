"""Joint Cross must review the merged revisions, including simultaneous edits."""

from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting import load_distribution_reporting_capability
from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_composition import (
    build_cross_owner_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_definitions import (
    register_cross_owner_pipeline_specializations,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CROSS_REVIEW_DIMENSIONS,
    CrossReviewFinding,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.kernel.conversations import ConversationRegistry
from manyselves.kernel.definitions import DefinitionKind
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.state_store import InMemoryWorkflowStateStore
from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink, WorkflowRuntimeHost


def _state(workspace: Path) -> dict:
    store = ReportingStore(workspace)
    state = {
        "run_id": "joint-swap",
        "module_submissions": {},
        "module_subject_refs": {},
        "module_review_completion_refs": {},
    }
    for owner, taxonomy in REPORT_TAXONOMY.items():
        module = ModuleSubmission(
            module_id=owner,
            revision=0,
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            submodule_narratives={
                key: f"合成观测 T，工况 A，数值 {'20' if owner == '2.1' else '10'}。"
                for key in taxonomy.submodules
            },
        )
        ref = f"Work/runs/joint-swap/modules/{owner}-r0.json"
        store.write_json(ref, module.model_dump(mode="json"))
        completion_ref = f"Work/runs/joint-swap/reviews/{owner}-completion.json"
        completion = ReviewCompletionRecord(
            lifecycle="module",
            run_id="joint-swap",
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key=f"module-auditor-{owner}",
            subject_refs=[ref],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        )
        store.write_json(completion_ref, completion.model_dump(mode="json"))
        state["module_submissions"][owner] = module
        state["module_subject_refs"][owner] = ref
        state["module_review_completion_refs"][owner] = completion_ref
    return state


def _finding(owner: str, *, advisory: bool = False) -> dict:
    return CrossReviewFinding(
        id=f"X-{owner}",
        owner_module_id=owner,
        target_submodule_ids=[next(iter(REPORT_TAXONOMY[owner].submodules))],
        related_module_ids=["2.2" if owner == "2.1" else "2.1"],
        category="facts",
        impact="advisory" if advisory else "blocking",
        observation="合成测试：同一观测对象与条件下两个模块的数值不一致，需要对照共同依据修正。",
        evidence_refs=[f"Work/runs/joint-swap/modules/{owner}-r0.json"],
        required_change="对照同一观测的共同依据修正当前模块数值，保留观测对象与条件，避免产生新的不一致。",
        reviewer_checks=["同一观测的两侧当前数值一致"],
    ).model_dump(mode="json")


class _JointScript:
    def __init__(self, mode: str):
        self.mode = mode
        self.calls = []
        self.snapshots = []

    async def invoke(self, agent, task, value, conversation, *, task_id):
        self.calls.append(task.id)
        if task.id == "cross-joint-runtime-review":
            contract = value.preparation.review_input
            self.snapshots.append(contract)
            coverage = [
                {"module_id": owner, "checked_dimensions": list(CROSS_REVIEW_DIMENSIONS)}
                for owner in REPORT_TAXONOMY
            ]
            if contract.phase == "initial":
                findings = (
                    []
                    if self.mode == "empty"
                    else [_finding("2.2", advisory=self.mode == "advice")]
                )
                if self.mode == "swap":
                    findings.insert(0, _finding("2.1"))
                payload = {
                    "kind": "cross_review_finding_submission",
                    "coverage": coverage,
                    "findings": findings,
                    "synthesis_inputs": [],
                }
            else:
                # Swapping values satisfies each old peer independently; the joint snapshot must expose both edits.
                if self.mode == "swap" and len(self.snapshots) == 2:
                    assert contract.module_revisions["2.1"] == 1
                    assert contract.module_revisions["2.2"] == 1
                    assert "10" in contract.modules["2.1"].submodule_narratives["2.1.1"]
                    assert "20" in contract.modules["2.2"].submodule_narratives["2.2.1.1"]
                verdicts = [
                    {
                        "finding_id": f.id,
                        "verdict": "escalate"
                        if self.mode == "escalate"
                        else "open"
                        if self.mode == "swap"
                        and f.owner_module_id == "2.2"
                        and len(self.snapshots) == 2
                        else "resolved",
                        "reason": "根据当前合并后的同一观测正文核对数值与条件，不依赖旧版本同行材料。",
                        "evidence_refs": list(contract.module_refs.values()),
                    }
                    for f in contract.required_findings
                ]
                payload = {
                    "kind": "cross_review_verdict_submission",
                    "coverage": coverage,
                    "verdicts": verdicts,
                    "new_findings": [_finding("2.3")]
                    if self.mode == "regression" and len(self.snapshots) == 2
                    else [],
                    "synthesis_inputs": [],
                }
        elif task.output_contract == "declarative_main_exception_agent_result":
            payload = {
                "kind": "workflow_decision_submission",
                "decision": "accept_dispute",
                "rationale": "合成测试保留争议及当前依据，通过明确的例外决策继续。",
                "finding_ids": value.main_preparation.exception_ids,
            }
        elif task.output_contract == "declarative_module_revision_agent_result":
            prep = value.revision.prepared
            owner = prep.module_id
            number = "20" if owner == "2.2" and prep.subject.revision == 0 else "10"
            payload = {
                "kind": "module_revision_submission",
                "module_id": owner,
                "base_revision": prep.subject.revision,
                "revision": prep.subject.revision + 1,
                "submodule_narratives": {
                    key: f"合成观测 T，工况 A，同一时点同一测点，数值 {number}。"
                    for key in prep.target_submodule_ids
                },
                "source_ids": [],
                "unresolved_questions": [],
                "revision_responses": [
                    {
                        "finding_id": key,
                        "action": "implemented",
                        "summary": "已经修改当前观测数值，提交合并后的共同版本进行复核。",
                        "changed_target_ids": prep.target_submodule_ids,
                    }
                    for key in prep.required_finding_ids
                ],
            }
        elif task.output_contract == "declarative_module_review_agent_result":
            prepared = value.review.prepared
            local_advice = {
                "id": f"M-{prepared.module_id}-{prepared.lifecycle_id}-r0-advice",
                "target_submodule_id": prepared.scope[0],
                "category": "editorial_quality",
                "impact": "advisory",
                "observation": "当前修订内容符合事实及任务要求，但可以选用更简洁的表达方式。",
                "evidence_refs": [prepared.subject_ref],
                "required_change": "后续编辑可以考虑精简表达，当前事实与结论不需要强制修订。",
                "reviewer_checks": ["建议仅涉及可选措辞，不影响事实正确性。"],
            }
            payload = {
                "kind": "module_review_finding_submission",
                "coverage": {
                    "submodule_ids": value.review.prepared.review_input.required_submodule_ids
                },
                "findings": [local_advice] if self.mode == "local_advice" else [],
            }
        else:
            raise AssertionError(f"unexpected task {task.id}")
        return AgentInvocationOutcome(
            status="ok",
            result={"status": "completed", "submission": payload},
            session_id=f"script:{conversation.key.value}",
        )

    async def invoke_with_recovery(self, *args, recovery_policy, **kwargs):
        return await self.invoke(*args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty", "advice", "local_advice", "swap", "regression", "escalate"])
async def test_joint_cross_closes_only_the_current_merged_snapshot(tmp_path: Path, mode: str):
    _, registry = load_distribution_reporting_capability()
    register_cross_owner_pipeline_specializations(registry)
    workflow = registry.require(DefinitionKind.WORKFLOW, "distribution-cross-joint-review")
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)
    runtime = CrossOwnerRuntime(tmp_path)
    script = _JointScript(mode)
    state = WorkflowState.for_plan(
        "joint-swap", plan, initial_variables={"reporting-state": _state(tmp_path)}
    )
    context = RuntimeContext(
        tools=build_cross_owner_tool_implementations(runtime),
        agents={
            key: script
            for key in [
                "cross-relation-reviewer",
                "evidence-auditor",
                "main-agent",
                *[f"module-{owner}-specialist" for owner in REPORT_TAXONOMY],
            ]
        },
        conversations=ConversationRegistry(),
    )
    final = await WorkflowRuntimeHost(
        build_builtin_executor_registry(), InMemoryWorkflowStateStore(), InMemoryWorkflowEventSink()
    ).execute(plan, state, context)
    assert final.status == "completed"
    result = final.outputs["result"]
    completion = ReviewCompletionRecord.model_validate_json(
        (tmp_path / result["cross_review_completion_ref"]).read_text(encoding="utf-8")
    )
    from manyselves.capabilities.distribution_reporting.runtime.review_artifacts import (
        load_current_review_completion,
    )

    loaded, _ = load_current_review_completion(
        workspace=tmp_path,
        run_id="joint-swap",
        completion_ref=result["cross_review_completion_ref"],
        lifecycle="cross",
        reviewer_agent_id="cross-relation-reviewer",
        reviewer_session_key="cross-joint",
        subject_refs=list(script.snapshots[-1].module_refs.values()),
    )
    assert loaded == completion
    assert completion.reviewer_session_key == "cross-joint"
    assert not list(
        (tmp_path / "Work/runs/joint-swap/reviews").glob("cross-owner-input-*-2.5.json")
    )

    assert len(script.calls) == {
        "swap": 9, "regression": 7, "escalate": 5, "local_advice": 4,
    }.get(mode, 1)
    assert set(script.snapshots[0].module_review_completion_refs) == set(REPORT_TAXONOMY)
    if mode == "swap":
        assert script.snapshots[-1].module_revisions == {
            "2.1": 1,
            "2.2": 2,
            "2.3": 0,
            "2.4": 0,
            "2.5": 0,
        }
        assert completion.subject_refs == list(script.snapshots[-1].module_refs.values())
        assert set(completion.resolved_finding_ids) == {"X-2.1", "X-2.2"}
    elif mode == "regression":
        assert script.snapshots[-1].module_revisions["2.3"] == 1
        assert set(completion.resolved_finding_ids) == {"X-2.2", "X-2.3"}
    elif mode == "escalate":
        assert result["review_exception_refs"]
    elif mode == "advice":
        assert completion.deferred_advisory_ids == ["X-2.2"]
        assert completion.resolved_finding_ids == []
    elif mode == "local_advice":
        ref = script.snapshots[-1].module_review_completion_refs["2.2"]
        local_completion, _ = load_current_review_completion(
            workspace=tmp_path, run_id="joint-swap", completion_ref=ref,
            lifecycle="module", reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.2",
            subject_refs=[script.snapshots[-1].module_refs["2.2"]],
        )
        assert len(local_completion.deferred_advisory_ids) == 1
        assert local_completion.resolved_finding_ids == []
        assert local_completion.verdict_refs == []
        assert script.snapshots[-1].module_revisions["2.2"] == 1


@pytest.mark.asyncio
async def test_joint_submit_preserves_verdict_identity_when_reordered(tmp_path):
    import json

    from manyselves.capabilities.distribution_reporting.runtime.collaboration_tools import (
        SubmitResultTool,
    )
    from manyselves.capabilities.distribution_reporting.runtime.cross_joint import prepare_review
    from manyselves.runtime.loops.bus import MessageBus

    state = _state(tmp_path)
    findings = [_finding("2.1"), _finding("2.2")]
    state["cross_joint"] = {
        "round": 1,
        "pending": findings,
        "synthesis_inputs": [],
        "changed_module_ids": ["2.1", "2.2"],
        "responses": {
            owner: [
                {
                    "finding_id": f"X-{owner}",
                    "action": "implemented",
                    "summary": "依据共同观测重新提交当前版本以供联合复审。",
                    "changed_target_ids": [next(iter(REPORT_TAXONOMY[owner].submodules))],
                }
            ]
            for owner in ["2.1", "2.2"]
        },
    }
    context = prepare_review(CrossOwnerRuntime(tmp_path), state)
    envelope = context.preparation.envelope
    tool = SubmitResultTool(
        agent_id=envelope.agent_id,
        session_id="joint",
        run_id=state["run_id"],
        task_id=envelope.task_id,
        store=ReportingStore(tmp_path),
        bus=MessageBus(),
        allowed_outputs=envelope.allowed_outputs,
        input_contract_kind=envelope.input_contract_kind,
        input_contract_ref=envelope.input_contract_ref,
    )
    await tool(
        kind="cross_review_verdict_submission",
        verdicts=[
            {
                "finding_id": "X-2.2",
                "verdict": "open",
                "reason": "当前第二模块的数值仍然不满足共同观测依据，需要继续修订。",
                "evidence_refs": [state["module_subject_refs"]["2.2"]],
            },
            {
                "finding_id": "X-2.1",
                "verdict": "resolved",
                "reason": "当前第一模块的数值与共同观测依据一致，满足原检查要求。",
                "evidence_refs": [state["module_subject_refs"]["2.1"]],
            },
        ],
        new_findings=[],
        synthesis_inputs=[],
    )
    result = json.loads(
        (tmp_path / f"Work/runs/{state['run_id']}/results/{envelope.task_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert {v["finding_id"]: v["verdict"] for v in result["payload"]["verdicts"]} == {
        "X-2.2": "open",
        "X-2.1": "resolved",
    }


def test_joint_repairs_preserve_report_scope_and_changed_input(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.cross_joint import (
        prepare_repairs,
        prepare_review,
    )

    state = _state(tmp_path)
    state["report_instruction"] = "只依据当前工况比较，不将已失效记录作为事实。"
    state["revision_input_changes"] = {"superseded_evidence_ids": ["E-old"]}
    runtime = CrossOwnerRuntime(tmp_path)
    joint = prepare_review(runtime, state)
    state["cross_joint"] = {"round": 1, "pending": [_finding("2.2")]}
    repairs = prepare_repairs(runtime, state)
    owner = repairs["cross_joint_repair_contexts"]["2.2"].preparation.owner_input
    assert (
        owner.report_instruction
        == joint.preparation.review_input.report_instruction
        == state["report_instruction"]
    )
    assert owner.input_changes == joint.preparation.review_input.input_changes
    assert owner.input_changes.superseded_evidence_ids == ["E-old"]
    assert set(repairs["cross_owner_inputs"]) == {"2.2"}
