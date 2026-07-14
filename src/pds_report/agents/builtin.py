from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pds_report.agents.base import AgentContext, AgentPort
from pds_report.domain.models import (
    Claim,
    CoverageEntry,
    CoverageMatrix,
    CoverageStatus,
    EvidenceItem,
    ModuleDraft,
    ModuleTask,
    OutputArtifact,
    ParsedArtifact,
    ParseStatus,
    ProjectManifest,
    ReportRequest,
    ReviewIssue,
    ReviewSeverity,
    SourceLocation,
)
from pds_report.infrastructure.intake import scan_project
from pds_report.infrastructure.project_store import ProjectStore


def _state(context: AgentContext, key: str, expected: type[object]) -> object:
    value = context.state[key]
    if not isinstance(value, expected):
        raise TypeError(f"{key} must be {expected.__name__}")
    return value


def _typed_list(context: AgentContext, key: str, expected: type[object]) -> list[object]:
    value = context.state[key]
    if not isinstance(value, list) or not all(isinstance(item, expected) for item in value):
        raise TypeError(f"{key} must be a list of {expected.__name__}")
    return value


@dataclass(slots=True)
class ManifestBuilderAgent:
    store: ProjectStore

    async def run(self, context: AgentContext) -> Mapping[str, object]:
        manifest = scan_project(context.project_root)
        self.store.write_json(Path("Work/manifest.json"), manifest)
        return {"project_manifest": manifest}


@dataclass(slots=True)
class ArtifactParserAgent:
    store: ProjectStore

    async def run(self, context: AgentContext) -> Mapping[str, object]:
        manifest = _state(context, "project_manifest", ProjectManifest)
        assert isinstance(manifest, ProjectManifest)
        artifacts: list[ParsedArtifact] = []
        for entry in manifest.files:
            if entry.parse_status is ParseStatus.UNSUPPORTED:
                continue
            source = SourceLocation(
                file_id=entry.id,
                relative_path=entry.relative_path,
            )
            path = context.project_root / entry.relative_path
            if entry.format in {"txt", "md", "csv", "json"}:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    entry.parse_status = ParseStatus.ERROR
                    entry.error = str(exc)
                    continue
                entry.parse_status = ParseStatus.PARSED
                artifacts.append(
                    ParsedArtifact(
                        id=f"artifact-{entry.id.removeprefix('file-')}",
                        source=source,
                        content_type="text",
                        data={"text": text},
                    )
                )
            else:
                artifacts.append(
                    ParsedArtifact(
                        id=f"artifact-{entry.id.removeprefix('file-')}",
                        source=source,
                        content_type="file-metadata",
                        data={"format": entry.format, "pending_parser": True},
                    )
                )
        self.store.write_json(Path("Work/manifest.json"), manifest)
        return {"project_manifest": manifest, "parsed_artifacts": artifacts}


@dataclass(slots=True)
class EvidenceNormalizerAgent:
    store: ProjectStore

    async def run(self, context: AgentContext) -> Mapping[str, object]:
        artifacts = _typed_list(context, "parsed_artifacts", ParsedArtifact)
        evidence_items: list[EvidenceItem] = []
        for artifact in artifacts:
            assert isinstance(artifact, ParsedArtifact)
            if artifact.content_type != "text":
                continue
            source_parts = artifact.source.relative_path.parts
            if not source_parts or source_parts[0] != "Inputs":
                continue
            text = artifact.data.get("text")
            if not isinstance(text, str):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                fact = line.strip()
                if not fact:
                    continue
                identity = sha256(f"{artifact.id}:{line_number}:{fact}".encode()).hexdigest()
                evidence_items.append(
                    EvidenceItem(
                        id=f"ev-{identity[:16]}",
                        fact=fact,
                        source=artifact.source.model_copy(
                            update={"locator": f"line {line_number}"}
                        ),
                    )
                )
        self.store.write_jsonl(Path("Work/evidence.jsonl"), evidence_items)
        return {"evidence_items": evidence_items}


@dataclass(slots=True)
class CoverageEvaluatorAgent:
    store: ProjectStore

    async def run(self, context: AgentContext) -> Mapping[str, object]:
        request = _state(context, "report_request", ReportRequest)
        evidence_items = _typed_list(context, "evidence_items", EvidenceItem)
        assert isinstance(request, ReportRequest)
        evidence_ids = [item.id for item in evidence_items if isinstance(item, EvidenceItem)]
        entries: list[CoverageEntry] = []
        for module_id in request.target_modules:
            if evidence_ids:
                status = CoverageStatus.READY
                gaps: list[str] = []
            elif request.allow_pending_evidence:
                status = CoverageStatus.PENDING
                gaps = ["当前项目没有可用于该模块的客户证据"]
            else:
                status = CoverageStatus.BLOCKED
                gaps = ["当前项目没有可用于该模块的客户证据"]
            entries.append(
                CoverageEntry(
                    module_id=module_id,
                    status=status,
                    evidence_ids=evidence_ids,
                    gaps=gaps,
                )
            )
        matrix = CoverageMatrix(entries=entries)
        self.store.write_json(Path("Work/coverage.json"), matrix)
        return {"coverage_matrix": matrix}


class ReportPlannerAgent:
    async def run(self, context: AgentContext) -> Mapping[str, object]:
        matrix = _state(context, "coverage_matrix", CoverageMatrix)
        assert isinstance(matrix, CoverageMatrix)
        tasks = [
            ModuleTask(
                module_id=entry.module_id,
                evidence_ids=entry.evidence_ids,
                audit_requirements=["每条确定性论断必须引用真实 evidence_id"],
            )
            for entry in matrix.entries
        ]
        return {"module_tasks": tasks}


class ModuleWorkerAgent:
    async def run(self, context: AgentContext) -> Mapping[str, object]:
        tasks = _typed_list(context, "module_tasks", ModuleTask)
        evidence_items = _typed_list(context, "evidence_items", EvidenceItem)
        evidence_by_id = {
            item.id: item for item in evidence_items if isinstance(item, EvidenceItem)
        }
        drafts: list[ModuleDraft] = []
        for task in tasks:
            assert isinstance(task, ModuleTask)
            evidence = [evidence_by_id[item_id] for item_id in task.evidence_ids]
            if evidence:
                claims = [
                    Claim(statement=item.fact, evidence_ids=[item.id]) for item in evidence
                ]
                pending: list[str] = []
            else:
                claims = [
                    Claim(
                        statement="该模块资料不足，无法形成确定性结论。",
                        pending_verification=True,
                    )
                ]
                pending = ["补充该模块对应的客户资料后重新分析"]
            drafts.append(
                ModuleDraft(
                    module_id=task.module_id,
                    claims=claims,
                    recommendations=["由主 Agent 根据证据状态协调下一步核验。"],
                    pending_verifications=pending,
                )
            )
        return {"module_drafts": drafts}


class EvidenceAuditorAgent:
    async def run(self, context: AgentContext) -> Mapping[str, object]:
        drafts = _typed_list(context, "module_drafts", ModuleDraft)
        evidence_items = _typed_list(context, "evidence_items", EvidenceItem)
        evidence_ids = {
            item.id for item in evidence_items if isinstance(item, EvidenceItem)
        }
        issues: list[ReviewIssue] = []
        for draft in drafts:
            assert isinstance(draft, ModuleDraft)
            for claim in draft.claims:
                unknown = set(claim.evidence_ids) - evidence_ids
                if unknown or (not claim.evidence_ids and not claim.pending_verification):
                    issues.append(
                        ReviewIssue(
                            module_id=draft.module_id,
                            claim_id=claim.id,
                            issue_type="missing_evidence",
                            severity=ReviewSeverity.BLOCKING,
                            blocking=True,
                            message="确定性论断缺少真实证据引用",
                            revision_request="删除论断或改为待核实状态",
                        )
                    )
                elif claim.pending_verification:
                    issues.append(
                        ReviewIssue(
                            module_id=draft.module_id,
                            claim_id=claim.id,
                            issue_type="pending_evidence",
                            severity=ReviewSeverity.WARNING,
                            message="该论断等待补充客户证据",
                        )
                    )
        return {"review_issues": issues}


class RevisionRouterAgent:
    async def run(self, context: AgentContext) -> Mapping[str, object]:
        request = _state(context, "report_request", ReportRequest)
        drafts = _typed_list(context, "module_drafts", ModuleDraft)
        issues = _typed_list(context, "review_issues", ReviewIssue)
        assert isinstance(request, ReportRequest)
        blocking_claims = {
            issue.claim_id
            for issue in issues
            if isinstance(issue, ReviewIssue) and issue.blocking and issue.claim_id
        }
        revised: list[ModuleDraft] = []
        for draft in drafts:
            assert isinstance(draft, ModuleDraft)
            copy = draft.model_copy(deep=True)
            if copy.revision < request.max_revision_rounds:
                changed = False
                for claim in copy.claims:
                    if claim.id in blocking_claims:
                        claim.pending_verification = True
                        changed = True
                if changed:
                    copy.revision += 1
                    copy.pending_verifications.append("审校退回：补充论断证据")
            revised.append(copy)
        return {"module_drafts": revised}


@dataclass(slots=True)
class ProjectDeliveryAgent:
    store: ProjectStore

    async def run(self, context: AgentContext) -> Mapping[str, object]:
        drafts = _typed_list(context, "module_drafts", ModuleDraft)
        issues = _typed_list(context, "review_issues", ReviewIssue)
        artifacts: list[OutputArtifact] = []
        module_ids: list[str] = []
        for draft in drafts:
            assert isinstance(draft, ModuleDraft)
            relative = Path("Outputs/Modules") / f"{draft.module_id}.json"
            self.store.write_json(relative, draft)
            artifacts.append(
                OutputArtifact(
                    kind="module_draft",
                    relative_path=relative,
                    source_ids=[claim.id for claim in draft.claims],
                )
            )
            module_ids.append(draft.module_id)

        review_relative = Path("Outputs/Reviews") / f"{context.run_id}.json"
        self.store.write_json(review_relative, issues)
        artifacts.append(
            OutputArtifact(
                kind="review_record",
                relative_path=review_relative,
                source_ids=[issue.id for issue in issues if isinstance(issue, ReviewIssue)],
            )
        )
        summary_relative = Path("Outputs/Reports") / f"{context.run_id}-summary.json"
        pending_modules = [
            draft.module_id
            for draft in drafts
            if isinstance(draft, ModuleDraft)
            and (
                draft.pending_verifications
                or any(claim.pending_verification for claim in draft.claims)
            )
        ]
        message = f"流程完成：模块 {', '.join(module_ids)} 草稿和审校记录已写入项目。"
        if pending_modules:
            message += f" 待核实模块：{', '.join(pending_modules)}；请补充客户证据后重试。"
        summary = {
            "message": message,
            "modules": module_ids,
            "pending_modules": pending_modules,
            "blocking_issue_count": sum(
                1 for issue in issues if isinstance(issue, ReviewIssue) and issue.blocking
            ),
        }
        self.store.write_json(summary_relative, summary)
        artifacts.append(
            OutputArtifact(kind="run_summary", relative_path=summary_relative)
        )
        return {"output_artifacts": artifacts, "run_summary": summary}


def build_builtin_agents(store: ProjectStore) -> dict[str, AgentPort]:
    return {
        "manifest-builder": ManifestBuilderAgent(store),
        "artifact-parser": ArtifactParserAgent(store),
        "evidence-normalizer": EvidenceNormalizerAgent(store),
        "coverage-evaluator": CoverageEvaluatorAgent(store),
        "report-planner": ReportPlannerAgent(),
        "module-worker": ModuleWorkerAgent(),
        "evidence-auditor": EvidenceAuditorAgent(),
        "revision-router": RevisionRouterAgent(),
        "project-delivery": ProjectDeliveryAgent(store),
    }
