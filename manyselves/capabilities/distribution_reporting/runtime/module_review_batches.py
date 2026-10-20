"""Module reviews whose input exceeds the request budget, in submodule batches.

A module review carries the module prose, Claims and every in-scope fact.
When that packet is above the retained input budget, the review is split into
consecutive groups of fixed submodules.  The same Auditor reviews each group
in its own turn of the same conversation, and the results are merged into one
review before the lane continues.  Batch inputs and results are files, so the
lane context stays small and a same-run resume skips reviewed batches.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleReviewCoverage,
    ModuleReviewFinding,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ModuleReviewInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    ModuleInitialReviewPreparation,
    ModuleRecheckPreparation,
    ModuleReviewBatch,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.config.schema import AgentDefaults
from manyselves.runtime.context_budget import estimate_text_tokens, retained_input_budget_for

T = TypeVar("T")
InputBudget = Callable[[], int] | int | None
Preparation = TypeVar("Preparation", ModuleInitialReviewPreparation, ModuleRecheckPreparation)


def resolve_input_budget(budget: InputBudget) -> int:
    """Estimated tokens one review turn may carry as its retained input."""

    if budget is None:
        return retained_input_budget_for(AgentDefaults())
    return int(budget() if callable(budget) else budget)


def contract_tokens(contract: Any) -> int:
    """Estimated tokens of a typed input as the Agent receives it inline."""

    return estimate_text_tokens(
        json.dumps(contract.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    )


def consecutive_batches(
    items: Sequence[T],
    measure: Callable[[list[T]], int],
    budget: int,
) -> list[list[T]]:
    """Consecutive groups of ``items`` whose measured input each fits ``budget``.

    A single item above the budget is still sent alone; the Agent runtime then
    stops with its context-budget reason instead of sending a request the
    Provider would reject.
    """

    items = list(items)
    if len(items) <= 1 or measure(items) <= budget:
        return [items]
    batches: list[list[T]] = []
    current: list[T] = []
    for item in items:
        candidate = [*current, item]
        if current and measure(candidate) > budget:
            batches.append(current)
            current = [item]
        else:
            current = candidate
    batches.append(current)
    return batches


def ordered_scope(module_id: str, scope: set[str]) -> list[str]:
    """Scope submodules in the module's fixed report order."""

    order = list(REPORT_TAXONOMY[module_id].submodules)
    return [item for item in order if item in scope] + sorted(scope - set(order))


def batch_ref(ref: str, index: int) -> str:
    return ref.removesuffix(".json") + f"-b{index + 1}.json"


def plan_batches(
    store: ReportingStore,
    *,
    module_id: str,
    scope: set[str],
    full_input: ModuleReviewInput,
    build_input: Callable[[set[str], int | None], ModuleReviewInput],
    input_ref: str,
    result_ref: str,
    budget: InputBudget,
    before_write: Callable[[int, set[str]], None] | None = None,
) -> list[ModuleReviewBatch]:
    """Write one input per submodule group when the full input is over budget.

    ``build_input`` receives the group and its batch index (``None`` while
    groups are only being measured).
    """

    limit = resolve_input_budget(budget)
    if contract_tokens(full_input) <= limit:
        return []
    groups = consecutive_batches(
        ordered_scope(module_id, scope),
        lambda part: contract_tokens(build_input(set(part), None)),
        limit,
    )
    if len(groups) <= 1:
        return []
    batches: list[ModuleReviewBatch] = []
    for index, group in enumerate(groups):
        part_ref = batch_ref(input_ref, index)
        if before_write is not None:
            before_write(index, set(group))
        store.write_json(part_ref, build_input(set(group), index).model_dump(mode="json"))
        part_result = batch_ref(result_ref, index)
        batches.append(ModuleReviewBatch(
            index=index,
            submodule_ids=list(group),
            input_ref=part_ref,
            result_ref=part_result,
            done=(store.workspace / part_result).is_file(),
        ))
    return batches


def batch_items(prepared: ModuleInitialReviewPreparation | ModuleRecheckPreparation | None) -> list[dict[str, int]]:
    """Positions the review loop visits; one position when nothing is batched."""

    count = len(prepared.batches) if prepared is not None and prepared.batches else 1
    return [{"batch_index": index} for index in range(count)]


def batch_index_from(value: Mapping[str, Any]) -> int:
    batch = value.get("batch") or {}
    return int(batch.get("batch_index", 0) if isinstance(batch, Mapping) else batch)


def _batch_envelope(
    full: TaskEnvelope,
    batch: ModuleReviewBatch,
    *,
    total: int,
    full_input_ref: str,
    target_submodule_ids: list[str],
) -> TaskEnvelope:
    modes = dict(full.artifact_delivery_modes)
    modes.pop(full_input_ref, None)
    modes[batch.input_ref] = "inline"
    return full.model_copy(update={
        "task_id": f"{full.task_id}-b{batch.index + 1}",
        "objective": (
            f"{full.objective}（第 {batch.index + 1}/{total} 批："
            f"{'、'.join(batch.submodule_ids)}）"
        ),
        "input_refs": [
            batch.input_ref if ref == full_input_ref else ref for ref in full.input_refs
        ],
        "artifact_delivery_modes": modes,
        "target_submodule_ids": target_submodule_ids,
        "input_contract_ref": batch.input_ref,
        "constraints": [
            *full.constraints,
            "本模块输入超过单次请求预算，按小节分批在同一会话中审查；本轮只审 "
            "required_submodule_ids 中的小节，其余小节在其他轮次审查，不要等待或推测它们。",
        ],
    })


def select_batch(
    store: ReportingStore,
    prepared: Preparation,
    index: int,
    *,
    target_submodule_ids: Callable[[ModuleReviewBatch], list[str]],
) -> Preparation:
    """Point the prepared review turn at one batch."""

    if not prepared.batches:
        return prepared
    batch = prepared.batches[index]
    full_envelope = prepared.full_envelope or prepared.envelope
    full_input_ref = prepared.full_review_input_ref or prepared.review_input_ref
    if full_envelope is None or full_input_ref is None:
        raise ValueError("batched module review has no full-scope turn")
    review_input = ModuleReviewInput.model_validate_json(
        (store.workspace / batch.input_ref).read_text(encoding="utf-8")
    )
    return prepared.model_copy(update={
        "batch_index": index,
        "full_envelope": full_envelope,
        "full_review_input_ref": full_input_ref,
        "review_input_ref": batch.input_ref,
        "review_input": review_input,
        "envelope": _batch_envelope(
            full_envelope,
            batch,
            total=len(prepared.batches),
            full_input_ref=full_input_ref,
            target_submodule_ids=target_submodule_ids(batch),
        ),
    })


def restore_full(store: ReportingStore, prepared: Preparation) -> Preparation:
    """The full-scope turn a merged batch review is recorded against."""

    if prepared.batch_index is None and prepared.full_envelope is None:
        return prepared
    full_input_ref = prepared.full_review_input_ref or prepared.review_input_ref
    review_input = (
        ModuleReviewInput.model_validate_json(
            (store.workspace / full_input_ref).read_text(encoding="utf-8")
        )
        if full_input_ref is not None
        else prepared.review_input
    )
    return prepared.model_copy(update={
        "batch_index": None,
        "envelope": prepared.full_envelope or prepared.envelope,
        "review_input_ref": full_input_ref,
        "review_input": review_input,
        "full_envelope": None,
        "full_review_input_ref": None,
    })


def mark_done(prepared: Preparation, index: int) -> Preparation:
    batches = list(prepared.batches)
    batches[index] = batches[index].model_copy(update={"done": True})
    return prepared.model_copy(update={"batches": batches})


def current_batch_needs_agent(prepared: ModuleInitialReviewPreparation | ModuleRecheckPreparation) -> bool:
    if prepared.batch_index is None:
        return True
    return not prepared.batches[prepared.batch_index].done


def write_batch_result(store: ReportingStore, batch: ModuleReviewBatch, payload: dict[str, Any]) -> None:
    """Persist one batch result; an identical rerun reuses it."""

    path = store.workspace / batch.result_ref
    if path.is_file():
        if json.loads(path.read_text(encoding="utf-8")) != payload:
            raise ValueError(f"refusing to overwrite module review batch result: {batch.result_ref}")
        return
    store.write_json(batch.result_ref, payload)


def _renumber(findings: list[ModuleReviewFinding], prefix: str, start: int) -> list[ModuleReviewFinding]:
    return [
        finding.model_copy(update={"id": f"{prefix}{start + offset:03d}"})
        for offset, finding in enumerate(findings)
    ]


def merge_finding_results(
    store: ReportingStore,
    batches: list[ModuleReviewBatch],
    *,
    prefix: str,
    scope: list[str],
) -> ModuleReviewFindingSubmission:
    """One initial review from every batch; ids are renumbered across batches."""

    findings: list[ModuleReviewFinding] = []
    for batch in batches:
        result = ModuleReviewFindingSubmission.model_validate_json(
            (store.workspace / batch.result_ref).read_text(encoding="utf-8")
        )
        findings.extend(_renumber(result.findings, prefix, len(findings) + 1))
    return ModuleReviewFindingSubmission(
        coverage=ModuleReviewCoverage(submodule_ids=list(scope)),
        findings=findings,
    )


def merge_verdict_results(
    store: ReportingStore,
    batches: list[ModuleReviewBatch],
    *,
    prefix: str,
    scope: list[str],
) -> ModuleReviewVerdictSubmission:
    """One recheck from every batch; new regression findings get fresh ids."""

    verdicts = []
    new_findings: list[ModuleReviewFinding] = []
    for batch in batches:
        result = ModuleReviewVerdictSubmission.model_validate_json(
            (store.workspace / batch.result_ref).read_text(encoding="utf-8")
        )
        verdicts.extend(result.verdicts)
        new_findings.extend(_renumber(result.new_findings, prefix, len(new_findings) + 1))
    return ModuleReviewVerdictSubmission(
        coverage=ModuleReviewCoverage(submodule_ids=list(scope)),
        verdicts=verdicts,
        new_findings=new_findings,
    )


__all__ = [
    "batch_index_from",
    "batch_items",
    "batch_ref",
    "consecutive_batches",
    "contract_tokens",
    "current_batch_needs_agent",
    "mark_done",
    "merge_finding_results",
    "merge_verdict_results",
    "ordered_scope",
    "plan_batches",
    "resolve_input_budget",
    "restore_full",
    "select_batch",
    "write_batch_result",
]
