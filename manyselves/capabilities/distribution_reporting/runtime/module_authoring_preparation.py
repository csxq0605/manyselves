"""Capability-owned preparation of the module Author task boundary.

This module is the single implementation of the deterministic inputs that a
module Author sees.  It deliberately stops at a typed lane context: invoking
an Agent, accepting a submission, and reviewing the result remain separate
file-defined actions.

The context marker and input-reference fingerprint in this module are the
existing draft-resume semantics moved out of the legacy runner.  No new
identity, retry, or validation policy is introduced here.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TEMPLATE_ROLE_SKILL_IDS,
    ModuleDispatchPlan,
    TaskEnvelope,
    TemplateSkillBoundaryManifest,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ModuleAuthoringInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleAuthoringPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.research.knowledge_context import (
    KnowledgeContextBuilder,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.user_supplements import (
    request_user_supplements,
    user_supplement_constraints,
)

TEMPLATE_SKILL_ROOT = Path("Work/report-template-role-skills")
TEMPLATE_SKILL_SOURCE = TEMPLATE_SKILL_ROOT / "source.json"


class ModuleAuthoringPreparationError(RuntimeError):
    """Error raised when the current run cannot form the Author contract."""


@dataclass(slots=True)
class ModuleAuthoringPreparation:
    """The non-serializable preparation result used by compatibility callers."""

    module_id: str
    state: dict[str, Any]
    workflow_id: str
    specialist_id: str
    envelope: TaskEnvelope
    resumed_payload: Any
    revision: int
    review: bool
    checkpoint: bool


def _request(state: dict[str, Any]) -> ReportRequest:
    request = state.get("request")
    if isinstance(request, ReportRequest):
        return request
    if isinstance(request, Mapping):
        request = ReportRequest.model_validate(request)
        state["request"] = request
    return request


def _dispatch(value: Any) -> Any:
    if isinstance(value, ModuleDispatchPlan):
        return value
    if isinstance(value, Mapping):
        return ModuleDispatchPlan.model_validate(value)
    # Compatibility callers historically supplied a structural object with
    # ``module_tasks``.  Preserve that boundary instead of adding a new
    # validation rule during the mechanical extraction.
    return value


def _sha256(path: Path) -> str:
    """Use the existing draft-artifact fingerprint semantics unchanged."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_template_skill(workspace: Path, state: dict[str, Any]) -> bool:
    """Load the fixed template Skill and its existing boundary manifest."""

    workspace = Path(workspace).resolve()
    refs = {
        skill_id: TEMPLATE_SKILL_ROOT / skill_id / "SKILL.md"
        for skill_id in TEMPLATE_ROLE_SKILL_IDS
    }
    boundary_ref = TEMPLATE_SKILL_ROOT / "boundary.json"
    required = [*refs.values(), boundary_ref, TEMPLATE_SKILL_SOURCE]
    missing = [
        path.as_posix()
        for path in required
        if not (workspace / path).is_file()
    ]
    if missing:
        raise ModuleAuthoringPreparationError(
            "固定模板写作 Skill 与当前边界契约不兼容，缺少文件："
            f"{', '.join(missing)}；请先单独运行 "
            "operation=distill_template_skill 更新固定 Skill"
        )
    try:
        boundary = TemplateSkillBoundaryManifest.model_validate_json(
            (workspace / boundary_ref).read_text(encoding="utf-8")
        )
        source_payload = json.loads(
            (workspace / TEMPLATE_SKILL_SOURCE).read_text(encoding="utf-8")
        )
        if not isinstance(source_payload, dict):
            raise ValueError("source.json must contain one JSON object")
        expected_hashes = {
            path.relative_to(TEMPLATE_SKILL_ROOT).as_posix(): _sha256(
                workspace / path
            )
            for path in [*refs.values(), boundary_ref]
        }
    except (OSError, ValueError, AttributeError) as exc:
        raise ModuleAuthoringPreparationError(
            "固定模板写作 Skill 的 boundary.json 或 source.json 无法解析；"
            "请先单独运行 operation=distill_template_skill 更新固定 Skill"
        ) from exc
    mismatched = [
        field
        for field, actual, expected in (
            (
                "boundary_policy_version",
                source_payload.get("boundary_policy_version"),
                boundary.policy_version,
            ),
            (
                "boundary_ref",
                source_payload.get("boundary_ref"),
                boundary_ref.as_posix(),
            ),
            (
                "artifact_sha256",
                source_payload.get("artifact_sha256"),
                expected_hashes,
            ),
        )
        if actual != expected
    ]
    if mismatched:
        raise ModuleAuthoringPreparationError(
            "固定模板写作 Skill 的 source.json 与当前边界契约或产物哈希不一致："
            f"{', '.join(mismatched)}；请先单独运行 "
            "operation=distill_template_skill 更新固定 Skill"
        )
    template_skill_text = {
        key: (workspace / path).read_text(encoding="utf-8")
        for key, path in refs.items()
    }
    poisoned = [
        refs[key].as_posix()
        for key, text in template_skill_text.items()
        if "<persisted_result_part" in text.casefold()
    ]
    if poisoned:
        raise ModuleAuthoringPreparationError(
            "固定模板写作 Skill 含有退休的内部历史令牌："
            f"{', '.join(poisoned)}；必须恢复完整正文或重新蒸馏，禁止把该标记"
            "继续注入报告 Agent"
        )
    state["template_skill_refs"] = {
        key: path.as_posix() for key, path in refs.items()
    }
    state["template_skill_text"] = template_skill_text
    state["template_skill_boundary"] = boundary
    return True


def _template_skill_context(state: Mapping[str, Any], skill_id: str) -> str:
    texts = state.get("template_skill_text", {})
    content = texts.get(skill_id, "") if isinstance(texts, Mapping) else ""
    if not content:
        return ""
    return (
        f'<template_role_skill id="{skill_id}" delivery_mode="inline">\n'
        f"{content}\n"
        "</template_role_skill>"
    )


def _domain_knowledge_context(text: str, provenance_ref: str) -> str:
    return (
        f'<domain_knowledge delivery_mode="inline" provenance_ref="{provenance_ref}" '
        'project_fact_authority="false">\n'
        f"{text}\n"
        "</domain_knowledge>"
    )


def module_author_inline_context(
    state: Mapping[str, Any],
    module_id: str,
    *,
    workspace: Path,
) -> str:
    """Build the current module's Knowledge + Author Skill inline context."""

    knowledge_refs = state.get("module_knowledge_refs", {})
    knowledge_ref = knowledge_refs[module_id]
    workspace = Path(workspace).resolve()
    knowledge_path = (workspace / knowledge_ref).resolve()
    if (
        not knowledge_path.is_relative_to(workspace)
        or not knowledge_path.is_file()
    ):
        raise ModuleAuthoringPreparationError(
            f"module {module_id} knowledge is not a readable workspace artifact"
        )
    return (
        _domain_knowledge_context(
            knowledge_path.read_text(encoding="utf-8"),
            str(knowledge_ref),
        )
        + "\n\n"
        + _template_skill_context(state, f"author-{module_id}")
    )


def _evidence_policy_constraints(policy: str) -> list[str]:
    if policy == "draft":
        return [
            "缺少客户证据的内容必须明确标注“资料不完整、待核实、低置信度”或等价限制，"
            "禁止写成已确认项目事实；不得因此跳过固定模块或子模块"
        ]
    if policy == "skip":
        return [
            "必须保留固定报告目录；缺少客户证据的子模块仅标注“未评估”，不得给出专业结论"
        ]
    return []


def build_module_dispatch(
    state: dict[str, Any],
    *,
    workspace: Path,
    store: ReportingStore,
    module_ids: Iterable[str] | None = None,
    global_root: Path | None = None,
    template_skill_loaded: bool = False,
) -> ModuleDispatchPlan:
    """Build the deterministic module dispatch consumed by Author lanes."""

    request = _request(state)
    selected_modules = tuple(module_ids or request.target_modules)
    knowledge = KnowledgeContextBuilder(
        Path(workspace),
        state["run_id"],
        global_root=global_root,
    )
    if not template_skill_loaded and not load_template_skill(workspace, state):
        raise ModuleAuthoringPreparationError(
            "fixed template-writing Skill must exist before module dispatch"
        )
    module_knowledge = {
        module_id: knowledge.build_module(module_id)
        for module_id in selected_modules
    }
    state["module_knowledge_refs"] = {
        module_id: context.path.as_posix()
        for module_id, context in module_knowledge.items()
    }
    knowledge_snapshot_refs = sorted(
        {
            context.snapshot_ref.as_posix()
            for context in module_knowledge.values()
            if context.snapshot_ref is not None
        }
    )
    state["knowledge_snapshot_refs"] = knowledge_snapshot_refs
    preparation_refs = state["preparation_refs"]
    tasks = [
        TaskEnvelope(
            task_id=f"module-{module_id}",
            run_id=state["run_id"],
            agent_id=f"module-{module_id}-specialist",
            objective=f"完成报告模块 {module_id}。用户要求：{request.instruction}",
            input_refs=[
                preparation_refs["coverage"],
                preparation_refs["evidence"],
                preparation_refs["manifest"],
                *(
                    [state["preparation_completion_ref"]]
                    if state.get("preparation_completion_ref")
                    else []
                ),
                state["module_knowledge_refs"][module_id],
                *knowledge_snapshot_refs,
                *(
                    [state["evidence_index_ref"]]
                    if state.get("evidence_index_ref")
                    else []
                ),
            ],
            constraints=[
                f"仅分析目标模块 {module_id}",
                "叶子任务内联当前叶子的项目/全局 Knowledge 增量与核心写作方法；整模块 Knowledge 和完整写作 Skill 以共享引用提供，仅在增量不足时按需读取",
                "R-* 是优先参考而非认知边界；可使用模型世界知识解释机理、备选原因和行业实践，但不能把它补成客户事实",
                "每个固定子模块必须形成带标题的完整正文，至少包含适用的现状、结论、风险机理和可执行建议",
                (
                    "固定 taxonomy 是唯一合法的数字标题体系；正文只允许使用本任务"
                    " required_submodule_ids 中的编号标题。现状、判断、原因、风险机理、"
                    "建议和验证只能使用普通段落或无编号粗体标签，禁止自行生成下一级编号"
                ),
                f"缺失证据策略={request.missing_evidence_policy}",
                *_evidence_policy_constraints(request.missing_evidence_policy),
                *request.execution_requirements,
                *user_supplement_constraints(
                    request.user_supplements,
                    stage="module_authoring",
                    target_ids={
                        module_id,
                        *REPORT_TAXONOMY[module_id].submodules,
                    },
                ),
            ],
            allowed_outputs=["module_submission"],
            target_submodule_ids=list(REPORT_TAXONOMY[module_id].submodules),
            inline_context=module_author_inline_context(
                state,
                module_id,
                workspace=workspace,
            ),
        )
        for module_id in selected_modules
    ]
    dispatch = ModuleDispatchPlan(
        module_tasks=tasks,
        rationale="Main 根据固定报告 taxonomy 和用户目标直接分配模块任务。",
    )
    store.write_run_model(state["run_id"], "workflow/module-dispatch.json", dispatch)
    state["module_dispatch"] = dispatch
    return dispatch


def module_authoring_context_sha256(
    state: Mapping[str, Any],
    module_id: str,
    *,
    workspace: Path,
) -> str:
    """Return the existing draft-context fingerprint for one module."""

    request = state.get("request")
    workspace = Path(workspace).resolve()

    def ref_record(ref: str | None) -> dict[str, str | None] | None:
        if not ref:
            return None
        path = (workspace / ref).resolve()
        return {
            "ref": ref,
            "sha256": (
                _sha256(path)
                if path.is_relative_to(workspace) and path.is_file()
                else None
            ),
        }

    planned = None
    dispatch = state.get("module_dispatch")
    if dispatch is not None:
        dispatch_model = _dispatch(dispatch)
        planned = next(
            (
                item.model_dump(mode="json")
                for item in dispatch_model.module_tasks
                if item.agent_id == f"module-{module_id}-specialist"
            ),
            None,
        )
    supplement_constraints = (
        user_supplement_constraints(
            request_user_supplements(request),
            stage="module_authoring",
            target_ids={module_id, *REPORT_TAXONOMY[module_id].submodules},
        )
    )
    preparation_refs = state.get("preparation_refs", {})
    payload = {
        "version": 3,
        "run_id": state["run_id"],
        "module_id": module_id,
        "required_submodule_ids": list(REPORT_TAXONOMY[module_id].submodules),
        "planned_task": planned,
        "execution_requirements": list(
            getattr(request, "execution_requirements", [])
        ),
        "missing_evidence_policy": getattr(
            request, "missing_evidence_policy", None
        ),
        "supplement_constraints": supplement_constraints,
        "coverage": ref_record(preparation_refs.get("coverage")),
        "evidence": ref_record(preparation_refs.get("evidence")),
        "manifest": ref_record(preparation_refs.get("manifest")),
        "knowledge": ref_record(
            state.get("module_knowledge_refs", {}).get(module_id)
        ),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def inherit_module_result_parts(
    workspace: Path,
    run_id: str,
    module_id: str,
    from_revision: int,
    to_revision: int,
) -> list[str]:
    """Seed a revision with the existing untouched Markdown parts."""

    if from_revision < 0 or to_revision <= from_revision:
        return []
    workspace = Path(workspace).resolve()
    draft_base = workspace / f"Work/runs/{run_id}/drafts/module-{module_id}"
    source_root = draft_base / f"r{from_revision}"
    target_root = draft_base / f"r{to_revision}"
    if not source_root.is_dir():
        return []
    inherited: list[str] = []
    target_root.mkdir(parents=True, exist_ok=True)
    for submodule_id in REPORT_TAXONOMY[module_id].submodules:
        source = source_root / f"{submodule_id}.md"
        target = target_root / f"{submodule_id}.md"
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
            inherited.append(submodule_id)
    return inherited


def prepare_module_authoring(
    module_id: str,
    state: dict[str, Any],
    workflow_id: str,
    *,
    workspace: Path,
    store: ReportingStore,
    review: bool = False,
    checkpoint: bool = False,
) -> ModuleAuthoringPreparation:
    """Prepare the full existing fresh/resume Author envelope."""

    request = _request(state)
    dispatch = _dispatch(state["module_dispatch"])
    state["module_dispatch"] = dispatch
    specialist_id = f"module-{module_id}-specialist"
    try:
        planned = next(
            item for item in dispatch.module_tasks if item.agent_id == specialist_id
        )
    except StopIteration as exc:
        raise ModuleAuthoringPreparationError(
            f"module dispatch has no task for {specialist_id}"
        ) from exc
    resumed_payload = state.get("specialist_submissions", {}).get(module_id)
    forced_fresh_revision: int | None = None
    revision = (
        resumed_payload.revision
        if resumed_payload is not None
        else 0
    )
    workspace = Path(workspace).resolve()
    if state.get("resume") and resumed_payload is None:
        draft_base = workspace / f"Work/runs/{state['run_id']}/drafts/module-{module_id}"
        persisted_revisions = [
            int(path.name[1:])
            for path in draft_base.glob("r*")
            if path.is_dir() and path.name[1:].isdigit()
        ]
        if persisted_revisions:
            revision = max(persisted_revisions)

    context_sha256 = module_authoring_context_sha256(
        state,
        module_id,
        workspace=workspace,
    )
    draft_base = workspace / f"Work/runs/{state['run_id']}/drafts/module-{module_id}"
    draft_root = draft_base / f"r{revision}"
    context_marker = draft_root / "_authoring-context.json"
    existing_parts = list(draft_root.glob("*.md"))
    marker_matches = False
    if context_marker.is_file():
        try:
            marker = json.loads(context_marker.read_text(encoding="utf-8"))
            marker_matches = marker.get("authoring_context_sha256") == context_sha256
        except (OSError, ValueError):
            marker_matches = False
    if existing_parts and not marker_matches:
        persisted_revisions = [
            int(path.name[1:])
            for path in draft_base.glob("r*")
            if path.is_dir() and path.name[1:].isdigit()
        ]
        revision = max([revision, *persisted_revisions]) + 1
        forced_fresh_revision = revision
        draft_root = draft_base / f"r{revision}"
        context_marker = draft_root / "_authoring-context.json"
    store.write_json(
        context_marker.relative_to(workspace).as_posix(),
        {
            "kind": "module_authoring_draft_context",
            "run_id": state["run_id"],
            "module_id": module_id,
            "revision": revision,
            "authoring_context_sha256": context_sha256,
        },
    )

    resume_part_constraints: list[str] = []
    resume_allowed_tools: list[str] = []
    saved_parts: list[str] = []
    rewrite_part_ids: list[str] = []
    base_constraints = list(
        dict.fromkeys(
            [
                *planned.constraints,
                (
                    "固定 taxonomy 是唯一合法的数字标题体系；正文只允许使用当前"
                    " required_submodule_ids 中的编号标题。现状、判断、原因、风险机理、"
                    "建议和验证只能使用普通段落或无编号粗体标签，禁止自行生成下一级编号"
                ),
                (
                    "共享模块会话中，若 Provider 支持同一轮多个工具调用，应在一个"
                    " assistant turn 内为所有 ready 小节分别调用 write_result_part，"
                    "再在下一轮提交小型 module commit；小节是文档 part，不是独立任务或会话"
                ),
                *request.execution_requirements,
                f"缺失证据策略={request.missing_evidence_policy}",
                *_evidence_policy_constraints(request.missing_evidence_policy),
                *user_supplement_constraints(
                    request.user_supplements,
                    stage="module_authoring",
                    target_ids={module_id, *REPORT_TAXONOMY[module_id].submodules},
                ),
            ]
        )
    )
    if forced_fresh_revision is not None:
        base_constraints.append(
            "输入或用户补充约束已变化；本 revision 必须从当前上下文完整重写全部固定小节，禁止复用旧 draft parts"
        )
    missing_parts: list[str] = []
    if state.get("resume"):
        if revision > 0 and forced_fresh_revision is None:
            inherit_module_result_parts(
                workspace,
                state["run_id"],
                module_id,
                revision - 1,
                revision,
            )
        saved_parts = sorted(path.stem for path in draft_root.glob("*.md"))
        missing_parts = sorted(
            set(REPORT_TAXONOMY[module_id].submodules) - set(saved_parts)
        )
        for part_id in saved_parts:
            part_path = draft_root / f"{part_id}.md"
            binding_path = draft_root / "_evidence" / f"{part_id}.json"
            binding_ready = False
            if binding_path.is_file():
                try:
                    binding = json.loads(binding_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    binding = None
                binding_ready = isinstance(binding, dict) and isinstance(
                    binding.get("evidence_ids"), list
                )
            part_content = part_path.read_text(encoding="utf-8")
            if (
                not binding_ready
                or "[[CLAIM:" in part_content
                or "<persisted_result_part" in part_content.casefold()
                or _extra_numbered_submodule_headings(part_id, part_content)
            ):
                rewrite_part_ids.append(part_id)
        resume_part_constraints = [
            "这是同一 run 的恢复任务；已有正文分段="
            + (", ".join(saved_parts) or "无"),
            "固定 taxonomy 尚缺正文分段="
            + (", ".join(missing_parts) or "无"),
            "当前协议只接收 write_result_part 逐项保存的完整读者可见正文和 evidence_ids；先用 list_result_parts 确认状态，ready 项不得重写。",
            "先调用 list_result_parts；必须重写或补绑定的 part="
            + (", ".join(rewrite_part_ids) or "无"),
        ]
        if saved_parts:
            resume_allowed_tools = [
                "search_project_evidence",
                "open_project_source",
                "list_result_parts",
                "write_result_part",
                "submit_result",
            ]

    base_constraints.append(
        "模块作者只在本模块会话内工作；不得调用 query_peer/reply_peer。跨模块关系由后续五个 Cross owner 处理。"
    )
    module_input = ModuleAuthoringInput(
        run_id=state["run_id"],
        module_id=module_id,
        revision=revision,
        required_submodule_ids=list(REPORT_TAXONOMY[module_id].submodules),
        coverage_ref=state["preparation_refs"]["coverage"],
        evidence_ref=state["preparation_refs"]["evidence"],
        manifest_ref=state["preparation_refs"]["manifest"],
        knowledge_ref=state["module_knowledge_refs"][module_id],
        saved_part_ids=saved_parts,
        rewrite_part_ids=rewrite_part_ids,
    )
    module_input_path = store.write_json(
        f"Work/runs/{state['run_id']}/context/module-authoring-{module_id}-r{revision}.json",
        module_input.model_dump(mode="json"),
    )
    module_input_ref = module_input_path.relative_to(workspace).as_posix()
    module_authoring_tools = [
        "search_project_evidence",
        "open_project_source",
        "search_reference_library",
        "open_reference",
        "web_search",
        "open_web_source",
        "inspect_document",
        "inspect_image",
        "calculate",
        "open_artifact",
        "search_text",
        "report_gap",
        "write_result_part",
        "list_result_parts",
        "report_blocked",
        "submit_result",
    ]
    envelope = TaskEnvelope.model_validate(
        planned.model_copy(
            update={
                "task_id": f"module-{module_id}",
                "run_id": state["run_id"],
                "agent_id": specialist_id,
                "allowed_outputs": ["module_submission"],
                "allowed_tools": (
                    resume_allowed_tools
                    if resume_allowed_tools
                    else module_authoring_tools
                ),
                "target_submodule_ids": (
                    sorted(set(rewrite_part_ids) | set(missing_parts))
                    if state.get("resume") and saved_parts
                    else list(REPORT_TAXONOMY[module_id].submodules)
                ),
                "constraints": list(
                    dict.fromkeys([*base_constraints, *resume_part_constraints])
                ),
                "revision": revision,
                "input_refs": [
                    module_input_ref,
                    module_input.coverage_ref,
                    module_input.evidence_ref,
                    module_input.manifest_ref,
                ],
                "input_contract_kind": "module_authoring_input",
                "input_contract_ref": module_input_ref,
                "inline_context": planned.inline_context or "",
                "artifact_delivery_modes": {
                    module_input_ref: "inline",
                    module_input.coverage_ref: "reference",
                    module_input.evidence_ref: "reference",
                    module_input.manifest_ref: "reference",
                },
            }
        ).model_dump(mode="python")
    )
    return ModuleAuthoringPreparation(
        module_id=module_id,
        state=state,
        workflow_id=workflow_id,
        specialist_id=specialist_id,
        envelope=envelope,
        resumed_payload=resumed_payload,
        revision=revision,
        review=review,
        checkpoint=checkpoint,
    )


def prepare_current_module_authoring(
    value: Any,
    *,
    workspace: Path,
    store: ReportingStore,
    global_root: Path | None = None,
) -> DeclarativeModuleRuntimeLaneContext:
    """Prepare a Capability lane's complete fresh or resumed Author turn."""

    context = (
        value
        if isinstance(value, DeclarativeModuleRuntimeLaneContext)
        else DeclarativeModuleRuntimeLaneContext.model_validate(value)
    )
    if context.status != "ready":
        return context
    state = context.reporting_state
    if "module_dispatch" not in state:
        build_module_dispatch(
            state,
            workspace=workspace,
            store=store,
            module_ids=(context.module_id,),
            global_root=global_root,
        )
    preparation = prepare_module_authoring(
        context.module_id,
        state,
        context.workflow_id,
        workspace=workspace,
        store=store,
    )
    authoring = DeclarativeModuleAuthoringPreparation(
        specialist_id=preparation.specialist_id,
        envelope=preparation.envelope,
        resumed_payload=preparation.resumed_payload,
        revision=preparation.revision,
        review=preparation.review,
        checkpoint=preparation.checkpoint,
    )
    return context.model_copy(
        deep=True,
        update={
            "reporting_state": preparation.state,
            "status": "author_resumed" if preparation.resumed_payload else "author_ready",
            "authoring": authoring,
            "error": None,
        },
    )


def resume_current_module_authoring(
    value: Any,
) -> DeclarativeModuleRuntimeLaneContext:
    """Project a same-run cached Author submission into the lane."""

    context = (
        value
        if isinstance(value, DeclarativeModuleRuntimeLaneContext)
        else DeclarativeModuleRuntimeLaneContext.model_validate(value)
    )
    if context.status != "author_resumed" or context.authoring is None:
        return context
    return context.model_copy(
        deep=True,
        update={
            "status": "authored",
            "authoring": None,
            "module": context.authoring.resumed_payload,
            "error": None,
        },
    )


def _extra_numbered_submodule_headings(
    submodule_id: str,
    narrative: str,
) -> tuple[str, ...]:
    """Keep the existing saved-part rewrite projection without Core imports."""

    import re

    numbered = re.compile(r"^#{1,6}\s+\d+(?:\.\d+)*\.?\s+")
    lines = narrative.splitlines()
    first_content = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    unexpected: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index == first_content and re.match(
            rf"^#{{1,6}}\s+{re.escape(submodule_id)}(?:\.|\s|$)",
            stripped,
        ):
            continue
        if numbered.match(stripped):
            unexpected.append(stripped)
    return tuple(unexpected)


__all__ = [
    "ModuleAuthoringPreparation",
    "ModuleAuthoringPreparationError",
    "TEMPLATE_SKILL_ROOT",
    "TEMPLATE_SKILL_SOURCE",
    "build_module_dispatch",
    "inherit_module_result_parts",
    "load_template_skill",
    "module_author_inline_context",
    "module_authoring_context_sha256",
    "prepare_current_module_authoring",
    "prepare_module_authoring",
    "resume_current_module_authoring",
]
