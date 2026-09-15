"""Deterministic impact analysis from frozen baseline vs current revision inputs."""

from __future__ import annotations

import re
from typing import Any

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
    resolve_submodule,
)

from .models.impact import (
    RevisionImpactAnalysis,
    RevisionImpactDecision,
    RevisionImpactEvidenceChange,
    RevisionImpactItem,
)
from .models.reporting import REPORT_MODULE_IDS
from .storage import ReportingStore

_E_REF = re.compile(r"\bE-\d{4,}\b")


def _evidence_ids_in_narratives(modules: dict[str, Any]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for module_id, raw in modules.items():
        narratives = raw.get("submodule_narratives") or {}
        for subsection_id, text in narratives.items():
            if not isinstance(text, str):
                continue
            found[subsection_id] = set(_E_REF.findall(text))
        for claim in raw.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            subsection_id = str(claim.get("submodule_id") or "")
            if not subsection_id:
                continue
            found.setdefault(subsection_id, set()).update(
                source for source in claim.get("source_ids") or [] if isinstance(source, str)
            )
    return found


def build_revision_impact_analysis(value: Any, *, store: ReportingStore) -> RevisionImpactAnalysis:
    """Map input inventory changes to concrete subsections using evidence bindings."""

    state = dict(value)
    run_id = str(state["run_id"])
    request = state.get("request") or {}
    if not isinstance(request, dict):
        request = request.model_dump(mode="json")
    baseline_id = str(state.get("baseline_run_id") or request.get("baseline_run_id") or "")
    instruction = str(request.get("instruction") or "")
    changes_payload = state.get("revision_input_changes") or {}
    file_changes = changes_payload.get("files") or state.get("input_changes") or {}
    added = changes_payload.get("current_evidence") or []
    superseded_ids = list(changes_payload.get("superseded_evidence_ids") or [])
    modules = state.get("module_submissions") or {}
    baseline_bindings = _evidence_ids_in_narratives(modules)

    change_rows: list[RevisionImpactEvidenceChange] = []
    added_paths: list[str] = []
    modified_paths: list[str] = []
    removed_paths: list[str] = []
    if isinstance(file_changes, dict) and (
        "added" in file_changes or "modified" in file_changes or "removed" in file_changes
    ):
        added_paths = [str(p) for p in file_changes.get("added") or []]
        modified_paths = [str(p) for p in file_changes.get("modified") or []]
        removed_paths = [str(p) for p in file_changes.get("removed") or []]
        for path in added_paths:
            change_rows.append(
                RevisionImpactEvidenceChange(
                    kind="file_added",
                    title=path,
                    locator=path,
                    summary=f"输入文件新增: {path}",
                )
            )
        for path in modified_paths:
            change_rows.append(
                RevisionImpactEvidenceChange(
                    kind="file_modified",
                    title=path,
                    locator=path,
                    summary=f"输入文件修改: {path}",
                )
            )
        for path in removed_paths:
            change_rows.append(
                RevisionImpactEvidenceChange(
                    kind="file_removed",
                    title=path,
                    locator=path,
                    summary=f"输入文件删除: {path}",
                )
            )
    else:
        for key, raw_detail in (file_changes or {}).items():
            detail = raw_detail if isinstance(raw_detail, dict) else {"kind": "modified"}
            kind = str(detail.get("kind") or "modified")
            mapped = {"added": "file_added", "modified": "file_modified", "removed": "file_removed"}.get(
                kind, "file_modified"
            )
            change_rows.append(
                RevisionImpactEvidenceChange(
                    kind=mapped,  # type: ignore[arg-type]
                    title=str(detail.get("path") or key),
                    locator=str(detail.get("path") or key),
                    summary=str(detail.get("summary") or f"输入文件{mapped}: {key}"),
                )
            )

    added_by_id: dict[str, Any] = {}
    for item in added:
        if isinstance(item, dict) and item.get("evidence_id"):
            added_by_id[str(item["evidence_id"])] = item
            change_rows.append(
                RevisionImpactEvidenceChange(
                    kind="added",
                    evidence_id=str(item["evidence_id"]),
                    title=str(item.get("title") or ""),
                    locator=str(item.get("locator") or ""),
                    summary=str(item.get("summary") or f"新增证据 {item['evidence_id']}"),
                )
            )

    current_evidence = [
        item
        for item in state.get("evidence_items") or []
        if isinstance(item, dict) and str(item.get("id", "")).startswith("E-")
    ]
    current_by_id = {str(item["id"]): item for item in current_evidence}
    for old_id in superseded_ids:
        change_rows.append(
            RevisionImpactEvidenceChange(
                kind="superseded",
                superseded_evidence_id=str(old_id),
                summary=f"基线证据 {old_id} 已被当前资料取代或删除",
            )
        )

    impacts: list[RevisionImpactItem] = []
    used_sections: set[str] = set()

    def add_impact(
        subsection_id: str,
        *,
        reason: str,
        instruction_text: str,
        confidence: str,
        evidence_ids: list[str],
        change_kinds: list[str],
    ) -> None:
        try:
            module_id = resolve_submodule(subsection_id).module_id
        except Exception:
            return
        existing = next((item for item in impacts if item.subsection_id == subsection_id), None)
        if existing is not None:
            merged_evidence = list(dict.fromkeys([*existing.evidence_ids, *evidence_ids]))
            merged_kinds = list(dict.fromkeys([*existing.change_kinds, *change_kinds]))
            reason = f"{existing.reason}；{reason}"
            if confidence == "high" or existing.confidence == "high":
                confidence = "high"
            impacts[impacts.index(existing)] = existing.model_copy(
                update={
                    "reason": reason,
                    "evidence_ids": merged_evidence,
                    "change_kinds": merged_kinds,
                    "confidence": confidence,  # type: ignore[arg-type]
                    "suggested_instruction": instruction_text
                    if change_kinds == ["evidence_superseded"]
                    else existing.suggested_instruction,
                }
            )
            return
        used_sections.add(subsection_id)
        impacts.append(
            RevisionImpactItem(
                subsection_id=subsection_id,
                module_id=module_id,
                reason=reason,
                suggested_instruction=instruction_text,
                confidence=confidence,  # type: ignore[arg-type]
                evidence_ids=evidence_ids,
                change_kinds=change_kinds,
            )
        )

    # 1) Explicit evidence module/submodule bindings for new evidence.
    for evidence_id, payload in added_by_id.items():
        item = current_by_id.get(evidence_id) or {}
        title = str(payload.get("title") or item.get("subject") or evidence_id)
        module_id = str(item.get("module_id") or "")
        subsection_id = str(item.get("submodule_id") or "")
        if subsection_id:
            add_impact(
                subsection_id,
                reason=f"当前资料新增/变更事实 {evidence_id}（{title}）绑定到该小节",
                instruction_text=(
                    f"根据当前资料 {evidence_id}（{title}）更新 {subsection_id} 的现状、"
                    "结论与建议；保留仍有效的历史内容，并标注未闭环或待核实边界。"
                ),
                confidence="high",
                evidence_ids=[evidence_id],
                change_kinds=["evidence_added"],
            )
        elif module_id:
            for subsection in REPORT_TAXONOMY[module_id].submodules:
                add_impact(
                    subsection,
                    reason=f"当前资料新增/变更事实 {evidence_id}（{title}）关联模块 {module_id}",
                    instruction_text=(
                        f"复核 {subsection} 是否需要吸收当前资料 {evidence_id}（{title}）；"
                        "仅在事实相关时改写，避免无依据扩写。"
                    ),
                    confidence="medium",
                    evidence_ids=[evidence_id],
                    change_kinds=["evidence_added"],
                )

    # 2) Superseded evidence that was cited by existing prose/claims.
    for old_id in superseded_ids:
        for subsection_id, cited in baseline_bindings.items():
            if old_id not in cited:
                continue
            add_impact(
                subsection_id,
                reason=f"原正文/断言引用了已被取代的证据 {old_id}",
                instruction_text=(
                    f"更新 {subsection_id} 中对 {old_id} 的引用：改用当前等价证据，"
                    "或明确该结论已失效/待核实；不得继续把 {old_id} 当作现行依据。"
                ).replace("{old_id}", old_id),
                confidence="high",
                evidence_ids=[old_id],
                change_kinds=["evidence_superseded"],
            )

    # 3) User seed requested_changes always remain first-class impacts.
    seed = request.get("requested_changes") or {}
    if isinstance(seed, dict):
        for subsection_id, text in seed.items():
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                resolve_submodule(str(subsection_id))
            except Exception:
                continue
            add_impact(
                str(subsection_id),
                reason="用户在启动修订时已明确要求修改该小节",
                instruction_text=text.strip(),
                confidence="high",
                evidence_ids=[],
                change_kinds=["user_requested"],
            )

    # 4) Instruction keywords as low-confidence fallback when nothing bound.
    notes_seed: list[str] = []
    if not impacts:
        mentioned_modules = {
            module_id
            for module_id in REPORT_MODULE_IDS
            if re.search(rf"(?<!\d){re.escape(module_id)}(?!\d)", instruction)
        }
        for module_id in mentioned_modules:
            for subsection in REPORT_TAXONOMY[module_id].submodules:
                add_impact(
                    subsection,
                    reason="用户指令点名了该模块，但当前资料未提供精确 evidence 绑定",
                    instruction_text=(
                        f"按用户指令复核并必要时修订 {subsection}；"
                        "只使用可追溯证据，缺少依据时明确不确定性。"
                    ),
                    confidence="low",
                    evidence_ids=[],
                    change_kinds=["instruction_hint"],
                )
        if not mentioned_modules and (added_paths or modified_paths or superseded_ids):
            notes_seed.append(
                "识别到输入变化，但未能映射到具体小节；请补充修改主题或在启动时提供 requested_changes。"
            )

    requested_changes = {item.subsection_id: item.suggested_instruction for item in impacts}
    raw_seeds = request.get("requested_changes") or {}
    user_seeds: dict[str, str] = {}
    if isinstance(raw_seeds, dict):
        for key, text in raw_seeds.items():
            if isinstance(text, str) and text.strip():
                user_seeds[str(key)] = text.strip()
    notes = [
        "影响分析基于冻结的基线业务快照与本次修订准备结果，不保证语义上零遗漏。",
        "Cross 仍负责修订后的跨模块一致性联动。",
        *notes_seed,
    ]
    if not impacts:
        notes.append("未识别到可映射的小节；请用户补充修改主题，或提供更明确的资料变化。")

    analysis = RevisionImpactAnalysis(
        run_id=run_id,
        baseline_run_id=baseline_id,
        instruction=instruction,
        changes=change_rows,
        impacts=impacts,
        requested_changes=requested_changes,
        user_seeds=user_seeds,
        notes=notes,
    )
    impact_ref = f"Work/runs/{run_id}/reviews/impact-analysis.json"
    store.write_json(impact_ref, analysis.model_dump(mode="json"))
    return analysis.model_copy(update={"impact_ref": impact_ref})


def impact_mode_enabled(value: Any) -> bool:
    state = dict(value)
    request = state.get("request") or {}
    if not isinstance(request, dict):
        request = request.model_dump(mode="json")
    return str(request.get("impact_mode") or "none") in {"auto", "confirm"}


def impact_needs_confirmation(value: Any) -> bool:
    state = dict(value)
    request = state.get("request") or {}
    if not isinstance(request, dict):
        request = request.model_dump(mode="json")
    return str(request.get("impact_mode") or "none") == "confirm"


def apply_revision_impact(value: Any) -> dict[str, Any]:
    """Replace revise-report targets with the accepted impact subset plus user seeds."""

    payload = dict(value)
    analysis_raw = payload.get("analysis") or payload
    decision_raw = payload.get("decision")
    if not decision_raw:
        decision_raw = {
            "kind": "revision_impact_decision",
            "action": "accept_all",
        }
    analysis = RevisionImpactAnalysis.model_validate(analysis_raw)
    decision = RevisionImpactDecision.model_validate(decision_raw)
    state = dict(payload.get("state") or {})
    if decision.action == "abort":
        raise ValueError("revision impact confirmation aborted by user")

    selected = {item.subsection_id: item for item in analysis.impacts}
    if decision.action == "accept_selected":
        keep = set(decision.selected_subsection_ids)
        unknown = sorted(keep - set(selected))
        if unknown:
            raise ValueError(f"impact decision selected unknown subsections: {unknown}")
        selected = {key: item for key, item in selected.items() if key in keep}

    request = state.get("request") or {}
    if not isinstance(request, dict):
        request = request.model_dump(mode="json")
    request = dict(request)
    # Seeds are the user's explicit requested_changes captured at impact-build time.
    # accept_selected must not re-expand to every impact already applied earlier.
    seeds = dict(analysis.user_seeds or state.get("impact_user_seeds") or {})

    merged: dict[str, str] = {}
    if decision.action == "accept_all":
        merged.update({k: v for k, v in seeds.items() if isinstance(v, str) and v.strip()})
    for subsection_id, item in selected.items():
        override = (decision.overridden_instructions or {}).get(subsection_id)
        merged[subsection_id] = (override or item.suggested_instruction).strip()
    if decision.action == "accept_selected":
        # Explicit user seeds remain only when the user did not drop them via
        # an impact item with the same subsection id.
        for subsection_id, text in seeds.items():
            if subsection_id in selected:
                continue
            if isinstance(text, str) and text.strip():
                merged[subsection_id] = text.strip()

    request["requested_changes"] = dict(merged)
    request["target_modules"] = sorted({".".join(key.split(".")[:2]) for key in merged})
    if not merged:
        raise ValueError("impact decision produced no requested_changes")

    state["request"] = request
    state["revision_targets"] = dict(merged)
    state["requested_modules"] = list(request["target_modules"])
    state["impact_analysis_ref"] = analysis.impact_ref
    state["impact_decision"] = decision.model_dump(mode="json")
    return state


def bind_revision_impact_tools(store: ReportingStore) -> dict[str, Any]:
    return {
        "prepare-revision-impact": lambda value: dict(value),
        "revision-impact-mode-enabled": impact_mode_enabled,
        "build-revision-impact": lambda value: build_revision_impact_analysis(value, store=store),
        "revision-impact-needs-confirmation": impact_needs_confirmation,
        "apply-revision-impact": apply_revision_impact,
    }


__all__ = [
    "apply_revision_impact",
    "bind_revision_impact_tools",
    "build_revision_impact_analysis",
    "impact_mode_enabled",
    "impact_needs_confirmation",
]
