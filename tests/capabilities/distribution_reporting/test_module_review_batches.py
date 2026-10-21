"""Module reviews above the request budget are reviewed in submodule batches."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime import module_review_batches
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleReviewFinding,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ModuleReviewInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.module_lane_tools import (
    module_recheck_requires_agent,
    module_review_requires_agent,
)
from manyselves.capabilities.distribution_reporting.runtime.module_recheck_tools import (
    accept_current_module_recheck,
    list_current_module_recheck_batches,
    merge_current_module_recheck_batches,
    prepare_current_module_recheck,
    select_current_module_recheck_batch,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_acceptance import (
    accept_current_module_review,
    list_current_module_review_batches,
    merge_current_module_review_batches,
    select_current_module_review_batch,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_batches import (
    contract_tokens,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (
    prepare_current_module_review,
)
from manyselves.capabilities.distribution_reporting.runtime.module_revision_tools import (
    accept_current_module_revision,
    prepare_current_module_revision,
)
from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
    PublicReportingWorkflowRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.interfaces.types import AgentResultMessage, UserMessage
from manyselves.kernel.workflow import WorkflowStatus
from manyselves.runtime.agent_execution import AgentExecutionService
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.state_store import InMemoryWorkflowStateStore
from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink
from tests.capabilities.distribution_reporting.test_public_reporting_runtime import (
    _agent_result_json,
    _BoundaryModuleRuntime,
    _patch_minimal_preparation,
)

_MODULE = "2.1"
_RUN = "module-batches"


def _module(module_id: str = _MODULE) -> ModuleSubmission:
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"### {submodule_id}\n\n" + "现场记录与分析正文。" * 200
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )


def _context() -> DeclarativeModuleRuntimeLaneContext:
    return DeclarativeModuleRuntimeLaneContext(
        module_id=_MODULE, workflow_id="public-reporting", status="authored",
        reporting_state={"run_id": _RUN}, module=_module(),
    )


def _budget_below_full_input(tmp_path: Path) -> int:
    full = prepare_current_module_review(
        _context(), store=ReportingStore(tmp_path / "measure"), input_budget=10**9,
    )
    return contract_tokens(full.review.prepared.review_input) - 1


def _finding(submodule_id: str, *, number: int = 1, round_number: int = 0) -> ModuleReviewFinding:
    return ModuleReviewFinding(
        id=f"M-{_MODULE}-initial-r{round_number}-{number:03d}",
        target_submodule_id=submodule_id, category="editorial_quality", impact="blocking",
        observation="Synthetic located finding for the module batch routing test.",
        evidence_refs=[f"Work/runs/{_RUN}/modules/{_MODULE}-r0.json"],
        required_change="Clarify the supported statement against the current evidence.",
        reviewer_checks=["Check against current evidence and the task requirements."],
    )


def _review_result(scope: list[str], findings: list[ModuleReviewFinding]) -> dict[str, Any]:
    return {"status": "completed", "submission": ModuleReviewFindingSubmission(
        coverage={"submodule_ids": scope}, findings=findings,
    )}


def test_review_within_budget_is_one_turn(tmp_path: Path) -> None:
    context = prepare_current_module_review(_context(), store=ReportingStore(tmp_path))
    assert context.review.prepared.batches == []
    assert list_current_module_review_batches(context) == [{"batch_index": 0}]
    assert select_current_module_review_batch(
        {"context": context, "batch": {"batch_index": 0}}, store=ReportingStore(tmp_path),
    ) == context


def test_oversized_review_splits_by_submodule_and_merges(tmp_path: Path) -> None:
    store = ReportingStore(tmp_path)
    budget = _budget_below_full_input(tmp_path)
    context = prepare_current_module_review(_context(), store=store, input_budget=budget)
    prepared = context.review.prepared
    batches = prepared.batches
    assert len(batches) >= 2
    order = list(REPORT_TAXONOMY[_MODULE].submodules)
    assert [s for batch in batches for s in batch.submodule_ids] == order
    for batch in batches:
        part = ModuleReviewInput.model_validate_json((tmp_path / batch.input_ref).read_text())
        assert part.required_submodule_ids == sorted(batch.submodule_ids)
        assert set(part.subject.submodule_narratives) == set(batch.submodule_ids)
        assert contract_tokens(part) <= budget or len(batch.submodule_ids) == 1
    assert module_review_requires_agent(context)

    task_ids = []
    for item in list_current_module_review_batches(context):
        context = select_current_module_review_batch(
            {"context": context, "batch": item}, store=store,
        )
        selected = context.review.prepared
        assert context.review.envelope.task_id.endswith(f"-b{item['batch_index'] + 1}")
        assert context.review.envelope.input_contract_ref == selected.review_input_ref
        assert selected.review_input.required_submodule_ids == sorted(
            batches[item["batch_index"]].submodule_ids
        )
        task_ids.append(context.review.envelope.task_id)
        assert module_review_requires_agent(context)
        # Each batch turn numbers its own findings from 001.
        first = batches[item["batch_index"]].submodule_ids[0]
        context = accept_current_module_review(
            {"context": context, "result": _review_result(
                selected.review_input.required_submodule_ids, [_finding(first)],
            )},
            store=store,
        )
        assert context.review.acceptance is None
        assert not module_review_requires_agent(context)
    assert len(set(task_ids)) == len(batches)

    merged = merge_current_module_review_batches(context, store=store)
    acceptance = merged.review.acceptance
    assert merged.status == "reviewed"
    assert acceptance.next_action == "revise"
    assert [f.id for f in acceptance.findings] == [
        f"M-{_MODULE}-initial-r0-{n:03d}" for n in range(1, len(batches) + 1)
    ]
    assert [f.target_submodule_id for f in acceptance.findings] == [
        batch.submodule_ids[0] for batch in batches
    ]
    recorded = json.loads((tmp_path / acceptance.finding_refs[0]).read_text())
    assert recorded["coverage"]["submodule_ids"] == sorted(order)
    assert merged.review.prepared.batch_index is None
    assert merged.review.prepared.review_input.required_submodule_ids == sorted(order)
    assert merged.review.envelope.task_id == f"module-{_MODULE}-initial-review-r0"


def test_same_run_resume_skips_reviewed_batches(tmp_path: Path) -> None:
    store = ReportingStore(tmp_path)
    budget = _budget_below_full_input(tmp_path)
    context = prepare_current_module_review(_context(), store=store, input_budget=budget)
    first = select_current_module_review_batch(
        {"context": context, "batch": {"batch_index": 0}}, store=store,
    )
    accept_current_module_review(
        {"context": first, "result": _review_result(
            first.review.prepared.review_input.required_submodule_ids, [],
        )},
        store=store,
    )

    resumed = prepare_current_module_review(_context(), store=store, input_budget=budget)
    assert [batch.done for batch in resumed.review.prepared.batches][:2] == [True, False]
    again = select_current_module_review_batch(
        {"context": resumed, "batch": {"batch_index": 0}}, store=store,
    )
    assert not module_review_requires_agent(again)
    second = select_current_module_review_batch(
        {"context": resumed, "batch": {"batch_index": 1}}, store=store,
    )
    assert module_review_requires_agent(second)


def test_recheck_batches_carry_only_their_findings(tmp_path: Path) -> None:
    store = ReportingStore(tmp_path)
    order = list(REPORT_TAXONOMY[_MODULE].submodules)
    context = prepare_current_module_review(_context(), store=store)
    findings = [_finding(order[0], number=1), _finding(order[-1], number=2)]
    context = accept_current_module_review(
        {"context": context, "result": _review_result(context.review.prepared.scope, findings)},
        store=store,
    )
    context = prepare_current_module_revision(context, store=store)
    context = accept_current_module_revision({"context": context, "result": {
        "status": "completed",
        "submission": ModuleRevisionSubmission(
            module_id=_MODULE, base_revision=0, revision=1,
            submodule_narratives={
                finding.target_submodule_id: f"### {finding.target_submodule_id}\n\n"
                + "修订后的正文与证据边界。" * 200
                for finding in findings
            },
            source_ids=[], unresolved_questions=[],
            revision_responses=[RevisionResponse(
                finding_id=finding.id, action="implemented",
                summary="已按实际证据明确说明修订内容和可以复核的边界。",
                changed_target_ids=[finding.target_submodule_id],
            ) for finding in findings],
        ),
    }}, store=store)

    # Measure the full recheck input in a copy: preparing records progress.
    copy = tmp_path.parent / f"{tmp_path.name}-measure"
    shutil.copytree(tmp_path, copy)
    full = prepare_current_module_recheck(context, store=ReportingStore(copy), input_budget=10**9)
    budget = contract_tokens(full.recheck.prepared.review_input) - 1
    context = prepare_current_module_recheck(context, store=store, input_budget=budget)
    batches = context.recheck.prepared.batches
    assert [batch.submodule_ids for batch in batches] == [[order[0]], [order[-1]]]
    items = list_current_module_recheck_batches(context)
    for item, finding in zip(items, findings, strict=True):
        context = select_current_module_recheck_batch(
            {"context": context, "batch": item}, store=store,
        )
        part = context.recheck.prepared.review_input
        assert [f.id for f in part.required_findings] == [finding.id]
        assert part.revision_diff_ref.endswith(f"-b{item['batch_index'] + 1}.json")
        assert (tmp_path / part.revision_diff_ref).is_file()
        assert module_recheck_requires_agent(context)
        context = accept_current_module_recheck({"context": context, "result": {
            "status": "completed",
            "submission": ModuleReviewVerdictSubmission(
                coverage={"submodule_ids": [finding.target_submodule_id]},
                verdicts=[ResolutionVerdict(
                    finding_id=finding.id, verdict="resolved",
                    reason="Checked the current subject against the original required correction.",
                    evidence_refs=[f"Work/runs/{_RUN}/modules/{_MODULE}-r1.json"],
                )],
            ),
        }}, store=store)
        assert not module_recheck_requires_agent(context)
    merged = merge_current_module_recheck_batches(context, store=store)
    assert merged.review.acceptance.next_action == "completed"
    assert merged.review.acceptance.resolved_ids == sorted(f.id for f in findings)
    assert merged.recheck.prepared.batch_index is None


class _BatchReviewerLoop:
    """Answers each Agent turn by reading its prepared input file."""

    def __init__(self, bus: MessageBus, runtime_id: str, workspace: Path, run_id: str) -> None:
        self.bus = bus
        self.runtime_id = runtime_id
        self.workspace = workspace
        self.run_id = run_id
        self.received: list[UserMessage] = []
        self._callback = None

    def restore_conversation(self, messages, *, task_boundaries=(), handoff_summary=None):
        del messages, task_boundaries, handoff_summary

    def _result(self, message: UserMessage) -> tuple[str, str, dict[str, Any]]:
        review_root = f"Work/runs/{self.run_id}/reviews/module/initial/2.4"
        action, _, batch = message.task_id.partition(":b")
        suffix = f"-b{batch}" if batch else ""
        if action == "invoke-current-module-reviewer":
            part = json.loads((self.workspace / f"{review_root}/input-r0{suffix}.json").read_text())
            scope = part["required_submodule_ids"]
            # The first two submodules each get one blocking finding.
            findings = [
                _finding(submodule_id).model_copy(update={
                    "id": "M-2.4-initial-r0-001", "evidence_refs": ["module-2.4-r0"],
                })
                for submodule_id in scope if submodule_id in {"2.4.1.1", "2.4.1.2"}
            ]
            return (
                f"module-2.4-initial-review-r0{suffix}",
                f"review{suffix}",
                {"kind": "module_review_finding_submission",
                 "coverage": {"submodule_ids": scope},
                 "findings": [f.model_dump(mode="json") for f in findings]},
            )
        if action == "invoke-current-module-recheck":
            part = json.loads((self.workspace / f"{review_root}/input-r1{suffix}.json").read_text())
            return (
                f"module-2.4-initial-review-r1{suffix}",
                f"recheck{suffix}",
                {"kind": "module_review_verdict_submission",
                 "coverage": {"submodule_ids": part["required_submodule_ids"]},
                 "verdicts": [{
                     "finding_id": finding["id"], "verdict": "resolved",
                     "reason": "修订内容已经满足原 finding 的证据边界与验证要求。",
                     "evidence_refs": ["module-2.4-r1"],
                 } for finding in part["required_findings"]],
                 "new_findings": []},
            )
        if action == "invoke-current-module-revision":
            return ("module-revision-r1-2.4", "revision", {
                "kind": "module_revision_submission", "module_id": "2.4",
                "base_revision": 0, "revision": 1,
                "submodule_narratives": {
                    "2.4.1.1": "修订后的正文明确说明证据边界、适用条件和可复核的后续验证方式。",
                    "2.4.1.2": "修订后的正文明确说明证据边界、适用条件和可复核的后续验证方式。",
                },
                "claims_upsert": [], "claim_ids_remove": [], "source_ids": [],
                "unresolved_questions": [],
                "revision_responses": [{
                    "finding_id": finding_id, "action": "implemented",
                    "summary": "已按要求补充证据边界和待核实限制，并保留可复核的验证步骤。",
                    "changed_target_ids": [target],
                } for finding_id, target in (
                    ("M-2.4-initial-r0-001", "2.4.1.1"), ("M-2.4-initial-r0-002", "2.4.1.2"),
                )],
            })
        submission = ModuleSubmission(
            module_id="2.4",
            submodule_narratives={
                s: f"### {s}\n\n" + "现场记录与分析正文。" * 200
                for s in REPORT_TAXONOMY["2.4"].submodules
            },
            claims=[], source_ids=[], unresolved_questions=[], revision=0,
        )
        return ("module-2.4-authoring", "author", submission.model_dump(mode="json"))

    async def start(self) -> None:
        async def respond(message: UserMessage) -> None:
            if message.agent_type != self.runtime_id:
                return
            self.received.append(message)
            terminal_task_id, name, payload = self._result(message)
            result_ref = f"Work/runs/{self.run_id}/results/{name}.json"
            path = self.workspace / result_ref
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_agent_result_json(
                task_id=terminal_task_id, run_id=self.run_id,
                agent_id=self.runtime_id.split(":", 2)[1], payload=payload,
            ), encoding="utf-8")
            await self.bus.publish(AgentResultMessage(
                sender=self.runtime_id.split(":", 2)[1], workflow_id=message.workflow_id,
                task_id=terminal_task_id, run_id=message.run_id, result_path=result_ref,
                task_attempt_id="", session_id=message.session_id,
            ))

        self._callback = respond
        self.bus.subscribe(UserMessage, respond)

    async def stop(self) -> None:
        if self._callback is not None:
            self.bus.unsubscribe(UserMessage, self._callback)

    async def wait_until_turn_complete(self) -> None:
        return None


@pytest.mark.asyncio
async def test_module_lane_reviews_and_rechecks_batches_in_one_reviewer_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_minimal_preparation(monkeypatch)
    # One submodule per batch: every review and recheck turn is batched.
    monkeypatch.setattr(module_review_batches, "resolve_input_budget", lambda _budget: 1)
    run_id = "module-lane-batches"
    snapshot = {"inventory_digest": "inventory", "files": [{
        "logical_ref": "Inputs/S4-6.xlsx", "snapshot_ref": "Inputs/S4-6.xlsx", "sha256": "a" * 64,
    }]}
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    execution = AgentExecutionService(bus, timeout=1)
    loops: list[_BatchReviewerLoop] = []

    def session_factory(runtime_id: str) -> _BatchReviewerLoop:
        loop = _BatchReviewerLoop(bus, runtime_id, tmp_path, run_id)
        loops.append(loop)
        return loop

    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: snapshot,
        snapshot_content=lambda source, target: (target, "a" * 64, "blob"),
        runtime_photo_ids=lambda _evidence, _photos: None,
        module_runtime=_BoundaryModuleRuntime(execution, session_factory),
        state_store=InMemoryWorkflowStateStore(),
        events=InMemoryWorkflowEventSink(),
    )
    request = ReportRequest(
        operation="module_report", instruction="run only module 2.4", target_modules=["2.4"],
        missing_evidence_policy="draft", preparation_mode="serial",
    )
    try:
        completed = await asyncio.wait_for(runtime.execute(request, run_id), timeout=60)
    finally:
        await execution.close_workflow("public-reporting")
        bus.shutdown()
        await bus_task

    assert completed.status is WorkflowStatus.COMPLETED
    reviewer = [loop for loop in loops if "evidence-auditor" in loop.runtime_id]
    assert len(reviewer) == 1
    review_turns = [
        m.task_id for m in reviewer[0].received
        if m.task_id.startswith("invoke-current-module-reviewer")
    ]
    recheck_turns = [
        m.task_id for m in reviewer[0].received
        if m.task_id.startswith("invoke-current-module-recheck")
    ]
    count = len(REPORT_TAXONOMY["2.4"].submodules)
    assert review_turns == [f"invoke-current-module-reviewer:b{n}" for n in range(1, count + 1)]
    assert recheck_turns == ["invoke-current-module-recheck:b1", "invoke-current-module-recheck:b2"]
    assert len({m.session_id for m in reviewer[0].received}) == 1
    root = tmp_path / f"Work/runs/{run_id}/reviews/module/initial/2.4"
    merged = json.loads((root / "findings-r0.json").read_text())
    assert [f["id"] for f in merged["findings"]] == ["M-2.4-initial-r0-001", "M-2.4-initial-r0-002"]
    assert (root / "verdicts-r1.json").is_file()
    assert (root / "completion-r1.json").is_file()


def test_revision_prompt_carries_only_target_submodules(tmp_path: Path) -> None:
    """The Author revision request does not repeat untouched submodules."""

    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_agent_bridge import (
        ModuleAuthoringAgentBridge,
    )
    from manyselves.kernel.definitions import DefinitionKind

    store = ReportingStore(tmp_path)
    order = list(REPORT_TAXONOMY[_MODULE].submodules)
    module = _module().model_copy(update={"submodule_narratives": {
        submodule_id: f"### {submodule_id}\n\nNARRATIVE-{submodule_id}" for submodule_id in order
    }})
    context = prepare_current_module_review(
        _context().model_copy(update={"module": module}), store=store,
    )
    context = accept_current_module_review(
        {"context": context, "result": _review_result(
            context.review.prepared.scope, [_finding(order[0])],
        )},
        store=store,
    )
    context = prepare_current_module_revision(context, store=store)
    _capability, registry = load_distribution_reporting_capability()
    prompt = ModuleAuthoringAgentBridge._prompt(
        object(),
        registry.require(DefinitionKind.AGENT, f"module-{_MODULE}-specialist"),
        registry.require(DefinitionKind.TASK, f"module-{_MODULE}-runtime-revision"),
        context,
    )
    assert f"NARRATIVE-{order[0]}" in prompt
    assert context.revision.prepared.subject_ref in prompt
    for untouched in order[1:]:
        assert f"NARRATIVE-{untouched}" not in prompt


def test_bound_module_runtime_owns_the_review_and_recheck_tools(tmp_path: Path) -> None:
    """The production tool map must use the module runtime's budget-aware tools."""
    class _ModuleRuntime:
        def prepare_review_lane(self, value):  # pragma: no cover - identity only
            return value

        def prepare_recheck_lane(self, value):  # pragma: no cover - identity only
            return value

        def list_review_batches(self, value):  # pragma: no cover - identity only
            return value

    module_runtime = _ModuleRuntime()
    runtime = PublicReportingWorkflowRuntime(
        tmp_path, input_snapshot=None, snapshot_content=None, runtime_photo_ids=None,
        module_runtime=module_runtime,
    )
    implementations = runtime._tool_implementations(ReportingStore(tmp_path))

    assert implementations["prepare-current-module-review"].__wrapped__ == module_runtime.prepare_review_lane
    assert implementations["prepare-current-module-recheck"].__wrapped__ == module_runtime.prepare_recheck_lane
    assert (
        implementations["list-current-module-review-batches"].__wrapped__
        == module_runtime.list_review_batches
    )
