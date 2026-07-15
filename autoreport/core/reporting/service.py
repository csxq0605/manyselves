"""Phase A reporting service executed inside the AutoReport runtime."""

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from openpyxl import load_workbook
from pydantic import BaseModel, ConfigDict, Field

from ...interfaces.types import AgentType, SystemNotice
from ..loops.bus import MessageBus
from ..tools.task_board import TaskBoard
from .config import AgentDefinition, load_packaged_workflow
from .models import (
    REPORT_MODULE_IDS,
    CoverageEntry,
    CoverageMatrix,
    CoverageStatus,
    EvidenceItem,
    ManifestFile,
    ModuleDraft,
    ModuleTask,
    OutputArtifact,
    ParsedArtifact,
    ProjectManifest,
    ReportRequest,
    ReviewIssue,
    SourceLocation,
)
from .store import ReportingStore


class ReportingRunResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    status: str
    phases: list[str] = Field(default_factory=list)
    output_paths: list[Path] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    error: str | None = None


class MissingEvidenceError(RuntimeError):
    def __init__(self, module_ids: list[str]):
        super().__init__(f"missing evidence for modules: {', '.join(module_ids)}")
        self.module_ids = module_ids


Handler = Callable[[dict], Awaitable[None]]


class ReportingService:
    """Run the packaged declarative workflow against the current project."""

    def __init__(self, workspace: Path, *, bus: MessageBus, task_board: TaskBoard):
        self.workspace = Path(workspace).resolve()
        self.bus = bus
        self.task_board = task_board
        self.store = ReportingStore(self.workspace)
        self.agents, self.workflow = load_packaged_workflow()
        self._handlers: dict[str, Handler] = {
            "manifest-builder": self._build_manifest,
            "artifact-parser": self._parse_artifacts,
            "evidence-normalizer": self._normalize_evidence,
            "coverage-evaluator": self._evaluate_coverage,
            "report-planner": self._plan_modules,
            "module-worker": self._draft_modules,
            "evidence-auditor": self._audit_evidence,
            "revision-router": self._route_revisions,
            "project-delivery": self._deliver,
        }

    async def run(self, request: ReportRequest) -> ReportingRunResult:
        self.store.ensure_layout()
        run_id = f"report-{uuid.uuid4().hex[:10]}"
        phases: list[str] = []
        state: dict = {"request": request, "run_id": run_id}
        await self._notice(f"配电报告流程 {run_id} 已启动。")

        try:
            for phase in self.workflow.phases:
                phases.append(phase.id)
                if phase.mode == "pipeline":
                    for agent_id in phase.agents:
                        await self._run_agent(self.agents[agent_id], state)
                else:
                    await asyncio.gather(
                        *(self._run_agent(self.agents[agent_id], state) for agent_id in phase.agents)
                    )
        except MissingEvidenceError as exc:
            result = ReportingRunResult(
                run_id=run_id,
                status="blocked",
                phases=phases,
                missing_evidence=exc.module_ids,
            )
            self._save_run(result)
            await self._notice(
                f"配电报告流程因缺少模块 {', '.join(exc.module_ids)} 的证据而阻塞。"
            )
            return result
        except Exception as exc:
            result = ReportingRunResult(
                run_id=run_id,
                status="failed",
                phases=phases,
                error=str(exc),
            )
            self._save_run(result)
            await self._notice(f"配电报告流程失败：{exc}")
            return result

        artifacts: list[OutputArtifact] = state.get("output_artifacts", [])
        result = ReportingRunResult(
            run_id=run_id,
            status="completed",
            phases=phases,
            output_paths=[self.workspace / artifact.path for artifact in artifacts],
        )
        self._save_run(result)
        await self._notice(
            "配电报告流程已完成："
            + ", ".join(str(path.relative_to(self.workspace)) for path in result.output_paths)
        )
        return result

    async def _run_agent(self, agent: AgentDefinition, state: dict) -> None:
        task = self.task_board.create_task(
            AgentType.MAIN,
            AgentType.MAIN,
            f"reporting:{agent.id}",
        )
        self.task_board.start_task(task.task_id, target_agent=AgentType.MAIN)
        try:
            await self._handlers[agent.id](state)
        except MissingEvidenceError:
            self.task_board.block_task(task.task_id, target_agent=AgentType.MAIN)
            raise
        except Exception:
            self.task_board.fail_task(task.task_id, target_agent=AgentType.MAIN)
            raise
        self.task_board.complete_task(task.task_id, target_agent=AgentType.MAIN)

    async def _notice(self, content: str) -> None:
        await self.bus.publish(SystemNotice(agent_type=AgentType.MAIN, content=content))

    def _save_run(self, result: ReportingRunResult) -> Path:
        return self.store.write_json(
            f"Work/runs/{result.run_id}.json",
            result.model_dump(mode="json"),
        )

    async def _build_manifest(self, state: dict) -> None:
        files: list[ManifestFile] = []
        ignored = {".git", ".autoreport", "Work", "Outputs", ".venv"}
        candidates = sorted(
            path
            for path in self.workspace.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".xlsx", ".xlsm"}
            and not (set(path.relative_to(self.workspace).parts) & ignored)
        )
        for index, path in enumerate(candidates, start=1):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files.append(
                ManifestFile(
                    id=f"file-{index:04d}",
                    path=path.relative_to(self.workspace),
                    sha256=digest,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            )
        manifest = ProjectManifest(files=files)
        state["project_manifest"] = manifest
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))

    async def _parse_artifacts(self, state: dict) -> None:
        manifest: ProjectManifest = state["project_manifest"]
        artifacts: list[ParsedArtifact] = []
        for manifest_file in manifest.files:
            try:
                workbook = load_workbook(
                    self.workspace / manifest_file.path,
                    read_only=True,
                    data_only=True,
                )
                for sheet in workbook.worksheets:
                    rows = list(sheet.iter_rows(values_only=True))
                    if not rows:
                        continue
                    headers = [str(value or f"column_{index + 1}") for index, value in enumerate(rows[0])]
                    for row_index, row in enumerate(rows[1:], start=2):
                        if not any(value not in (None, "") for value in row):
                            continue
                        artifacts.append(
                            ParsedArtifact(
                                id=f"artifact-{len(artifacts) + 1:04d}",
                                kind="workbook_row",
                                source=SourceLocation(
                                    file_id=manifest_file.id,
                                    path=manifest_file.path,
                                    sheet=sheet.title,
                                    cell=f"A{row_index}",
                                ),
                                payload={"headers": headers, "values": list(row)},
                            )
                        )
                workbook.close()
                manifest_file.parse_status = "parsed"
            except Exception as exc:
                manifest_file.parse_status = "failed"
                manifest_file.error = str(exc)
        state["parsed_artifacts"] = artifacts
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))

    async def _normalize_evidence(self, state: dict) -> None:
        evidence: list[EvidenceItem] = []
        for artifact in state.get("parsed_artifacts", []):
            pairs = [
                f"{header}={value}"
                for header, value in zip(
                    artifact.payload["headers"], artifact.payload["values"], strict=False
                )
                if value not in (None, "")
            ]
            if not pairs:
                continue
            evidence.append(
                EvidenceItem(
                    id=f"ev-{len(evidence) + 1:04d}",
                    subject=str(artifact.payload["values"][0]),
                    fact="; ".join(pairs),
                    source=artifact.source,
                )
            )
        state["evidence_items"] = evidence
        self.store.write_jsonl(
            "Work/evidence.jsonl",
            [item.model_dump(mode="json") for item in evidence],
        )

    async def _evaluate_coverage(self, state: dict) -> None:
        request: ReportRequest = state["request"]
        evidence: list[EvidenceItem] = state.get("evidence_items", [])
        entries: dict[str, CoverageEntry] = {}
        for module_id in REPORT_MODULE_IDS:
            if module_id not in request.target_modules:
                entries[module_id] = CoverageEntry(
                    module_id=module_id,
                    status=CoverageStatus.PENDING,
                    gaps=["本轮未请求"],
                )
            elif evidence:
                entries[module_id] = CoverageEntry(
                    module_id=module_id,
                    status=CoverageStatus.READY,
                    evidence_ids=[item.id for item in evidence],
                )
            else:
                entries[module_id] = CoverageEntry(
                    module_id=module_id,
                    status=CoverageStatus.BLOCKED,
                    gaps=["未找到可解析的客户工作簿证据"],
                )
        coverage = CoverageMatrix(entries=entries)
        state["coverage_matrix"] = coverage
        self.store.write_json("Work/coverage.json", coverage.model_dump(mode="json"))

    async def _plan_modules(self, state: dict) -> None:
        request: ReportRequest = state["request"]
        coverage: CoverageMatrix = state["coverage_matrix"]
        blocked = [
            module_id
            for module_id in request.target_modules
            if coverage.entries[module_id].status is CoverageStatus.BLOCKED
        ]
        if blocked:
            raise MissingEvidenceError(blocked)
        state["module_tasks"] = [
            ModuleTask(
                id=f"module-{module_id}",
                module_id=module_id,
                evidence_ids=coverage.entries[module_id].evidence_ids,
            )
            for module_id in request.target_modules
        ]

    async def _draft_modules(self, state: dict) -> None:
        evidence_by_id = {item.id: item for item in state.get("evidence_items", [])}
        drafts: list[ModuleDraft] = []
        for task in state.get("module_tasks", []):
            lines = [f"# {task.module_id} 配电现状分析", ""]
            for evidence_id in task.evidence_ids:
                item = evidence_by_id[evidence_id]
                lines.append(
                    f"- {item.fact} `[{item.id}: {item.source.path}#{item.source.sheet}!{item.source.cell}]`"
                )
            drafts.append(
                ModuleDraft(
                    module_id=task.module_id,
                    markdown="\n".join(lines) + "\n",
                    evidence_ids=task.evidence_ids,
                )
            )
        state["module_drafts"] = drafts

    async def _audit_evidence(self, state: dict) -> None:
        known = {item.id for item in state.get("evidence_items", [])}
        issues: list[ReviewIssue] = []
        for draft in state.get("module_drafts", []):
            missing = sorted(set(draft.evidence_ids) - known)
            if missing:
                issues.append(
                    ReviewIssue(
                        module_id=draft.module_id,
                        kind="unknown_evidence",
                        message=f"未知证据引用：{', '.join(missing)}",
                        severity="blocking",
                    )
                )
        state["review_issues"] = issues

    async def _route_revisions(self, state: dict) -> None:
        blocking = [
            issue.module_id
            for issue in state.get("review_issues", [])
            if issue.severity == "blocking"
        ]
        if blocking:
            raise RuntimeError(f"evidence audit blocked modules: {', '.join(blocking)}")

    async def _deliver(self, state: dict) -> None:
        output_artifacts: list[OutputArtifact] = []
        for draft in state.get("module_drafts", []):
            relative = Path("Outputs") / "Modules" / f"{draft.module_id}.md"
            self.store.write_text(relative.as_posix(), draft.markdown)
            output_artifacts.append(
                OutputArtifact(kind="module", path=relative, module_id=draft.module_id)
            )
        review_path = self.store.write_json(
            "Outputs/Reviews/phase-a.json",
            {
                "issues": [
                    issue.model_dump(mode="json") for issue in state.get("review_issues", [])
                ]
            },
        )
        output_artifacts.append(
            OutputArtifact(kind="review", path=review_path.relative_to(self.workspace))
        )
        state["output_artifacts"] = output_artifacts
