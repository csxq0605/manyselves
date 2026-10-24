"""Single-step ports for joint Cross review and parallel owner repairs.

The YAML workflow owns dispatch, concurrency, joins and iteration. These ports
project one merged snapshot and retain the existing author/local-audit lifecycle.
"""

from functools import partial
from typing import Any

from .cross_owner_runtime import (
    REPORT_MODULE_IDS,
    _build_owner_input,
    _metadata_map,
    _module_subject_ref,
    _module_submissions,
    _state_from,
)
from .main_exception import accept_main_exception_decision, prepare_main_exception_decision
from .models.agentic import (
    CROSS_REVIEW_DIMENSIONS,
    CrossDecisionPack,
    CrossOwnerFindingSubmission,
    CrossReviewCoverageEntry,
    CrossReviewFinding,
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    ResolutionVerdict,
    RevisionResponse,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from .models.cross_owner import (
    CrossJointPreparation,
    DeclarativeCrossJointAgentResult,
    DeclarativeCrossJointRuntimeContext,
    DeclarativeCrossOwnerInitialAgentResult,
    DeclarativeCrossOwnerRuntimeContext,
    DeclarativeMainExceptionAgentResult,
    DeclarativeMainExceptionUserInput,
)
from .models.inputs import (
    CrossReviewInput,
    ReviewCompletionRecord,
    module_content_view,
    report_instruction_from_state,
)


def _progress(state: dict) -> dict:
    return state.setdefault(
        "cross_joint",
        {
            "round": 0,
            "findings": [],
            "pending": [],
            "verdicts": {},
            "finding_refs": [],
            "verdict_refs": [],
            "resolved_ids": [],
            "responses": {},
            "changed_module_ids": list(REPORT_MODULE_IDS),
            "synthesis_inputs": [],
        },
    )


def prepare_review(runtime, value: Any) -> DeclarativeCrossJointRuntimeContext:
    from .assessment_context import module_rule_source_refs, source_observation_groups

    state = _state_from(value)
    progress = _progress(state)
    modules = _module_submissions(state)
    refs = {
        key: _module_subject_ref(state, _metadata_map(state), modules[key])
        for key in REPORT_MODULE_IDS
    }
    review_round = progress["round"]
    contract = CrossReviewInput(
        phase="initial" if review_round == 0 else "recheck",
        run_id=state["run_id"],
        review_round=review_round,
        report_instruction=report_instruction_from_state(state),
        input_changes=state.get("revision_input_changes"),
        module_refs=refs,
        module_revisions={key: modules[key].revision for key in REPORT_MODULE_IDS},
        modules={key: module_content_view(modules[key]) for key in REPORT_MODULE_IDS},
        source_observation_groups=source_observation_groups(runtime.store.workspace, state["run_id"]),
        changed_module_ids=progress["changed_module_ids"],
        required_findings=progress["pending"],
        revision_responses_by_module=progress["responses"],
        module_review_completion_refs=dict(state.get("module_review_completion_refs", {})),
        local_regression_review_refs=(
            {
                key: state["module_review_completion_refs"][key]
                for key in progress["changed_module_ids"]
            }
            if review_round
            else {}
        ),
        prior_synthesis_inputs=progress["synthesis_inputs"],
    )
    ref = f"Work/runs/{state['run_id']}/reviews/cross-joint-input-r{review_round}.json"
    runtime.store.write_json(ref, contract.model_dump(mode="json"))
    rule_refs = sorted({ref for module in modules.values() for ref in module_rule_source_refs(module)})
    envelope = TaskEnvelope(
        task_id=f"cross-joint-r{review_round}",
        run_id=state["run_id"],
        agent_id="cross-relation-reviewer",
        objective="审查当前合并快照的跨模块关系；将实质缺陷分派给真正出错的模块，按共同当前版本复审。",
        input_refs=[ref, *refs.values(), *rule_refs],
        input_contract_kind="cross_review_input",
        input_contract_ref=ref,
        allowed_tools=["search_text", "open_artifact", "submit_result"],
        allowed_outputs=[
            "cross_review_finding_submission"
            if review_round == 0
            else "cross_review_verdict_submission"
        ],
        artifact_delivery_modes={ref: "inline", **{path: "reference" for path in [*refs.values(), *rule_refs]}},
    )
    return DeclarativeCrossJointRuntimeContext(
        preparation=CrossJointPreparation(
            run_id=state["run_id"],
            workflow_id=runtime.workflow_id,
            review_round=review_round,
            review_input=contract,
            envelope=envelope,
        )
    )


def accept_review(runtime, value: Any) -> dict:
    state = _state_from(value["state"])
    context = DeclarativeCrossJointRuntimeContext.model_validate(value["context"])
    result = DeclarativeCrossJointAgentResult.model_validate(value["result"])
    if result.status != "completed" or result.submission is None:
        raise ValueError(result.error or "Joint Cross did not submit a result")
    progress = _progress(state)
    submission = result.submission
    review_round = progress["round"]
    if review_round == 0:
        if not isinstance(submission, CrossReviewFindingSubmission):
            raise ValueError("Initial joint Cross requires findings")
        new_findings = submission.findings
        progress["pending"] = []
        progress["escalated"] = []
        finding_ref = f"Work/runs/{state['run_id']}/reviews/cross-joint-findings-r0.json"
        runtime.store.write_json(finding_ref, submission.model_dump(mode="json"))
        progress["finding_refs"].append(finding_ref)
    else:
        if not isinstance(submission, CrossReviewVerdictSubmission):
            raise ValueError("Joint Cross recheck requires verdicts")
        pending = {f["id"]: f for f in progress["pending"]}
        if {v.finding_id for v in submission.verdicts} != set(pending):
            raise ValueError("Joint Cross verdicts must cover exactly the pending findings")
        progress["pending"] = [
            pending[v.finding_id] for v in submission.verdicts if v.verdict != "resolved"
        ]
        progress["resolved_ids"] = sorted(
            set(progress["resolved_ids"])
            | {v.finding_id for v in submission.verdicts if v.verdict == "resolved"}
        )
        progress["verdicts"].update(
            {v.finding_id: v.model_dump(mode="json") for v in submission.verdicts}
        )
        progress["escalated"] = [
            v.model_dump(mode="json") for v in submission.verdicts if v.verdict == "escalate"
        ]
        verdict_ref = (
            f"Work/runs/{state['run_id']}/reviews/cross-joint-verdicts-r{review_round}.json"
        )
        runtime.store.write_json(verdict_ref, submission.model_dump(mode="json"))
        progress["verdict_refs"].append(verdict_ref)
        new_findings = submission.new_findings
        if new_findings:
            finding_ref = (
                f"Work/runs/{state['run_id']}/reviews/cross-joint-regression-r{review_round}.json"
            )
            runtime.store.write_json(
                finding_ref,
                CrossReviewFindingSubmission(
                    coverage=submission.coverage,
                    findings=new_findings,
                ).model_dump(mode="json"),
            )
            progress["finding_refs"].append(finding_ref)
    existing_ids = {f["id"] for f in progress["findings"]}
    if any(f.id in existing_ids for f in new_findings):
        raise ValueError("Joint Cross new finding reuses an immutable finding id")
    progress["findings"].extend(f.model_dump(mode="json") for f in new_findings)
    progress["pending"].extend(
        f.model_dump(mode="json") for f in new_findings if f.impact == "blocking"
    )
    progress["synthesis_inputs"] = [s.model_dump(mode="json") for s in submission.synthesis_inputs]
    progress["subject_refs"] = context.preparation.review_input.module_refs
    return state


def needs_repairs(_runtime, value: Any) -> bool:
    return bool(_progress(_state_from(value))["pending"])


def prepare_repairs(runtime, value: Any) -> dict:
    state = _state_from(value)
    progress = _progress(state)
    state["cross_review_round"] = progress["round"]
    modules = _module_submissions(state)
    metadata = _metadata_map(state)
    state["cross_owner_inputs"] = {}
    state["cross_owner_input_refs"] = {}
    active_owners = {finding["owner_module_id"] for finding in progress["pending"]}
    for owner in REPORT_MODULE_IDS:
        if owner not in active_owners:
            continue
        contract, ref = _build_owner_input(
            state=state,
            modules=modules,
            metadata=metadata,
            owner_module_id=owner,
            review_round=progress["round"],
            phase="repair",
        )
        contract = type(contract).model_validate(
            {
                **contract.model_dump(mode="json"),
                "report_instruction": report_instruction_from_state(state),
                "input_changes": state.get("revision_input_changes"),
            }
        )
        runtime.store.write_json(ref, contract.model_dump(mode="json"))
        state["cross_owner_inputs"][owner] = contract
        state["cross_owner_input_refs"][owner] = ref
    contexts = {}
    for owner in REPORT_MODULE_IDS:
        if owner not in active_owners:
            contexts[owner] = DeclarativeCrossOwnerRuntimeContext(
                owner_module_id=owner,
                status="round_completed",
            )
            continue
        # Seed owner authoring from the joint decision; no owner reviewer is invoked.
        context = runtime.prepare_initial({"state": state, "owner_module_id": owner})
        findings = [
            CrossReviewFinding.model_validate(f)
            for f in progress["pending"]
            if f["owner_module_id"] == owner
        ]
        seeded = CrossOwnerFindingSubmission(
            owner_module_id=owner,
            coverage=CrossReviewCoverageEntry(
                module_id=owner, checked_dimensions=list(CROSS_REVIEW_DIMENSIONS)
            ),
            findings=findings,
        )
        if context.acceptance is None:
            context = runtime.accept_initial(
                {
                    "context": context,
                    "result": DeclarativeCrossOwnerInitialAgentResult(
                        status="completed", submission=seeded
                    ),
                }
            )
        contexts[owner] = context
    state["cross_joint_repair_contexts"] = contexts
    return state


def repair_context(_runtime, value: Any) -> DeclarativeCrossOwnerRuntimeContext:
    state = _state_from(value["state"])
    return DeclarativeCrossOwnerRuntimeContext.model_validate(
        state["cross_joint_repair_contexts"][value["owner_module_id"]]
    )


def merge_repairs(runtime, value: Any) -> dict:
    state = _state_from(value["state"])
    progress = _progress(state)
    responses = {}
    changed = []
    modules = _module_submissions(state)
    state["module_subject_refs"] = {
        owner: _module_subject_ref(state, _metadata_map(state), modules[owner])
        for owner in REPORT_MODULE_IDS
    }
    for owner in REPORT_MODULE_IDS:
        context = DeclarativeCrossOwnerRuntimeContext.model_validate(value["outcomes"][owner])
        if context.status == "failed":
            raise ValueError(context.error or "Cross repair failed")
        if not runtime.initial_has_findings(context):
            continue
        local = context.local_review_acceptance
        if (
            local is None
            or local.review.next_action != "completed"
            or not local.review.completion_ref
        ):
            raise ValueError("Joint Cross repair must finish its local module audit")
        module = local.review.current
        ref = f"Work/runs/{state['run_id']}/modules/{owner}-r{module.revision}.json"
        runtime.store.write_json(ref, module.model_dump(mode="json"))
        state["module_submissions"][owner] = module
        state.setdefault("specialist_submissions", {})[owner] = module
        state.setdefault("module_subject_refs", {})[owner] = ref
        state.setdefault("module_review_completion_refs", {})[owner] = local.review.completion_ref
        state.setdefault("review_exception_refs", []).extend(context.review_exception_refs)
        responses[owner] = [r.model_dump(mode="json") for r in local.cross_responses]
        changed.append(owner)
    progress["responses"] = responses
    progress["changed_module_ids"] = changed
    progress["round"] += 1
    state.pop("cross_joint_repair_contexts", None)
    return state


def prepare_exception(runtime, value: Any) -> DeclarativeCrossJointRuntimeContext:
    state = _state_from(value)
    progress = _progress(state)
    context = DeclarativeCrossJointRuntimeContext()
    if progress.get("escalated"):
        context.main_preparation = prepare_main_exception_decision(
            store=runtime.store,
            run_id=state["run_id"],
            workflow_id=runtime.workflow_id,
            scope="cross",
            subject_refs=list(progress["subject_refs"].values()),
            finding_refs=progress["finding_refs"],
            verdicts=[ResolutionVerdict.model_validate(v) for v in progress["escalated"]],
            responses=[
                RevisionResponse.model_validate(r)
                for rows in progress["responses"].values()
                for r in rows
            ],
        )
        if context.main_preparation.mode == "continue_existing":
            context.main_acceptance = accept_main_exception_decision(
                store=runtime.store,
                preparation=context.main_preparation,
                result=None,
                raise_for_terminal_decisions=False,
            )
    return context


def exception_requires_agent(_runtime, value: Any) -> bool:
    context = DeclarativeCrossJointRuntimeContext.model_validate(value)
    return context.main_preparation is not None and context.main_acceptance is None


def accept_exception(runtime, value: Any) -> DeclarativeCrossJointRuntimeContext:
    context = DeclarativeCrossJointRuntimeContext.model_validate(value["context"])
    result = DeclarativeMainExceptionAgentResult.model_validate(value["result"])
    if result.status != "completed" or result.submission is None:
        raise ValueError(result.error or "Main joint exception failed")
    context.main_acceptance = accept_main_exception_decision(
        store=runtime.store,
        preparation=context.main_preparation,
        result=result.submission,
        raise_for_terminal_decisions=False,
    )
    return context


def exception_requests_user(_runtime, value: Any) -> bool:
    context = DeclarativeCrossJointRuntimeContext.model_validate(value)
    return bool(
        context.main_acceptance and context.main_acceptance.result.decision == "request_user"
    )


def apply_exception_user_input(runtime, value: Any) -> DeclarativeCrossJointRuntimeContext:
    context = DeclarativeCrossJointRuntimeContext.model_validate(value["context"])
    supplied = DeclarativeMainExceptionUserInput.model_validate(value["input"])
    return accept_exception(
        runtime,
        {
            "context": context,
            "result": DeclarativeMainExceptionAgentResult(
                status="completed",
                submission=WorkflowDecisionSubmission(
                    decision=supplied.decision,
                    rationale=supplied.rationale,
                    finding_ids=context.main_preparation.exception_ids,
                ),
            ),
        },
    )


def apply_exception(_runtime, value: Any) -> dict:
    state = _state_from(value["state"])
    progress = _progress(state)
    context = DeclarativeCrossJointRuntimeContext.model_validate(value["context"])
    if context.main_acceptance is not None:
        accepted = context.main_acceptance
        if accepted.result.decision in {"stop_incomplete", "request_user"}:
            raise ValueError(accepted.result.rationale)
        state.setdefault("review_exception_refs", []).append(accepted.decision_ref)
        if accepted.result.decision == "accept_dispute":
            ids = set(accepted.result.finding_ids)
            progress["pending"] = [f for f in progress["pending"] if f["id"] not in ids]
            progress["resolved_ids"] = sorted(set(progress["resolved_ids"]) | ids)
    return state


def complete(runtime, value: Any) -> dict:
    state = _state_from(value)
    progress = _progress(state)
    if progress["pending"]:
        raise ValueError("Joint Cross has unclosed corrective findings")
    advice = [
        f
        for f in progress["findings"]
        if f["impact"] == "advisory" and f["id"] not in progress["resolved_ids"]
    ]
    ref = f"Work/runs/{state['run_id']}/reviews/cross-completion.json"
    completion = ReviewCompletionRecord(
        review_protocol_version=3,
        lifecycle="cross",
        run_id=state["run_id"],
        reviewer_agent_id="cross-relation-reviewer",
        reviewer_session_key="cross-joint",
        subject_refs=[progress["subject_refs"][key] for key in REPORT_MODULE_IDS],
        finding_refs=progress["finding_refs"],
        verdict_refs=progress["verdict_refs"],
        resolved_finding_ids=progress["resolved_ids"],
        deferred_advisory_ids=[f["id"] for f in advice],
    )
    runtime.store.write_json(ref, completion.model_dump(mode="json"))
    pack = CrossDecisionPack(
        version=2,
        run_id=state["run_id"],
        module_ids=list(REPORT_MODULE_IDS),
        cross_review_completion_ref=ref,
        synthesis_inputs=progress["synthesis_inputs"],
        deferred_advisories=advice,
    )
    pack_ref = f"Work/runs/{state['run_id']}/reviews/cross-decision-pack.json"
    runtime.store.write_json(pack_ref, pack.model_dump(mode="json"))
    state.update(
        cross_review_completion_ref=ref,
        cross_decision_pack_ref=pack_ref,
        cross_synthesis_inputs=pack.synthesis_inputs,
    )
    return state


PORTS = {
    "prepare-joint-cross-review": prepare_review,
    "accept-joint-cross-review": accept_review,
    "joint-cross-needs-repairs": needs_repairs,
    "prepare-joint-cross-repairs": prepare_repairs,
    "project-joint-cross-repair-context": repair_context,
    "merge-joint-cross-repairs": merge_repairs,
    "prepare-joint-cross-exception": prepare_exception,
    "joint-cross-exception-requires-agent": exception_requires_agent,
    "accept-joint-cross-exception": accept_exception,
    "joint-cross-exception-requests-user": exception_requests_user,
    "apply-joint-cross-exception-user-input": apply_exception_user_input,
    "apply-joint-cross-exception": apply_exception,
    "complete-joint-cross-review": complete,
}


def build_joint_tools(runtime):
    return {name: partial(function, runtime) for name, function in PORTS.items()}
