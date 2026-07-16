"""Complete V2 reporting service executed inside the AutoReport runtime."""

import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ...config.schema import AgentDefaults
from ...interfaces.types import AgentType, SystemNotice
from ..loops.bus import MessageBus
from ..providers.base import LLMProvider
from ..tools.task_board import TaskBoard
from .agent_runner import ReportingAgentRunner
from .config import load_packaged_workflow
from .coverage import evaluate_coverage
from .intake.adapters import IntakeAdapterRegistry
from .intake.manifest import build_manifest
from .intake.wps_images import extract_wps_images
from .mappers import map_s2_1, map_s4_4, map_s4_6
from .models import (
    EvidenceItem,
    OutputArtifact,
    ParsedArtifact,
    PhotoAsset,
    ProjectManifest,
    ReportRequest,
)
from .request_gate import ReportingBlockedError
from .store import ReportingStore
from .workflow import ReportWorkflowRunner


class ReportingRunResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    status: str
    phases: list[str] = Field(default_factory=list)
    output_paths: list[Path] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    error: str | None = None


class ReportingService:
    """Run the provider-backed multi-agent workflow against the current project."""

    def __init__(
        self,
        workspace: Path,
        *,
        bus: MessageBus,
        task_board: TaskBoard,
        llm_provider: LLMProvider,
        agent_defaults: AgentDefaults | None = None,
    ):
        if llm_provider is None:
            raise ValueError(
                "ReportingService requires an LLM provider; offline report generation is not supported"
            )
        self.workspace = Path(workspace).resolve()
        self.bus = bus
        self.task_board = task_board
        self.llm_provider = llm_provider
        self.agent_defaults = agent_defaults or AgentDefaults()
        self.store = ReportingStore(self.workspace)
        self.agents, self.workflow = load_packaged_workflow()
        template_root = Path(__file__).resolve().parents[2] / "templates" / "reporting"
        self.report_template_path = template_root / "report_template.docx"

    async def run(self, request: ReportRequest) -> ReportingRunResult:
        self.store.ensure_layout()
        run_id = f"report-{uuid.uuid4().hex[:10]}"
        phases: list[str] = []
        state: dict = {"request": request, "run_id": run_id}
        await self._notice(f"配电报告流程 {run_id} 已启动。")

        try:
            phases.extend(phase.id for phase in self.workflow.phases)
            runner = ReportWorkflowRunner(
                self,
                ReportingAgentRunner(
                    self.workspace,
                    self.bus,
                    self.llm_provider,
                    self.agent_defaults,
                ),
            )
            await runner.run(state)
        except ReportingBlockedError as exc:
            result = ReportingRunResult(
                run_id=run_id,
                status="blocked",
                phases=phases,
                missing_evidence=exc.missing_evidence,
            )
            self._save_run(result)
            await self._notice("配电报告流程等待补资或用户确认：" + ", ".join(exc.missing_evidence))
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

    async def _notice(self, content: str) -> None:
        await self.bus.publish(SystemNotice(agent_type=AgentType.MAIN, content=content))

    def _save_run(self, result: ReportingRunResult) -> Path:
        return self.store.write_json(
            f"Work/runs/{result.run_id}.json",
            result.model_dump(mode="json"),
        )

    async def _build_manifest(self, state: dict) -> None:
        manifest = build_manifest(self.workspace)
        state["project_manifest"] = manifest
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))

    async def _parse_artifacts(self, state: dict) -> None:
        manifest: ProjectManifest = state["project_manifest"]
        artifacts: list[ParsedArtifact] = []
        registry = IntakeAdapterRegistry()
        for manifest_file in manifest.files:
            if manifest_file.purpose in {"s2-1", "s4-4", "s4-6"}:
                continue
            try:
                artifacts.extend(registry.parse(self.workspace / manifest_file.path, manifest_file))
                manifest_file.parse_status = "parsed"
            except Exception as exc:
                manifest_file.parse_status = "failed"
                manifest_file.error = str(exc)
        state["parsed_artifacts"] = artifacts
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))

    async def _normalize_evidence(self, state: dict) -> None:
        evidence: list[EvidenceItem] = []
        manifest: ProjectManifest = state["project_manifest"]
        photo_assets: list[PhotoAsset] = []
        mapping_gaps: list[dict] = []
        mappers = {
            "s2-1": map_s2_1,
            "s4-4": map_s4_4,
            "s4-6": map_s4_6,
        }
        for manifest_file in manifest.files:
            mapper = mappers.get(manifest_file.purpose or "")
            if mapper is None:
                continue
            input_path = self.workspace / manifest_file.path
            try:
                if manifest_file.purpose == "s4-4":
                    extracted = extract_wps_images(
                        input_path,
                        output_dir=self.workspace / "Work" / "assets" / manifest_file.id,
                    )
                    photo_assets.extend(
                        asset.model_copy(update={"path": asset.path.relative_to(self.workspace)})
                        for asset in extracted.values()
                    )
                mapped = mapper(input_path, file_id=manifest_file.id)
                evidence.extend(
                    item.model_copy(
                        update={
                            "source": item.source.model_copy(update={"path": manifest_file.path})
                        }
                    )
                    for item in mapped.evidence_items
                )
                mapping_gaps.extend(
                    {
                        "file_id": manifest_file.id,
                        **gap.model_dump(mode="json"),
                    }
                    for gap in mapped.gaps
                )
                manifest_file.parse_status = "parsed"
            except Exception as exc:
                manifest_file.parse_status = "failed"
                manifest_file.error = str(exc)

        for artifact in state.get("parsed_artifacts", []):
            if artifact.kind == "manual_required":
                mapping_gaps.append(
                    {
                        "file_id": artifact.source.file_id,
                        "kind": "manual_required",
                        **artifact.payload,
                    }
                )
                continue
            if artifact.kind == "workbook_row":
                pairs = [
                    f"{header}={value}"
                    for header, value in zip(
                        artifact.payload["headers"],
                        artifact.payload["values"],
                        strict=False,
                    )
                    if value not in (None, "")
                ]
                if not pairs:
                    continue
                subject = str(artifact.payload["values"][0])
                fact = "; ".join(pairs)
            elif artifact.kind in {"text", "document_paragraph", "pdf_page"}:
                fact = str(artifact.payload.get("text", "")).strip()
                if not fact:
                    continue
                subject = artifact.source.path.name
            elif artifact.kind == "image_metadata":
                subject = artifact.source.path.name
                fact = (
                    f"图片元数据：{artifact.payload['width']}x{artifact.payload['height']}，"
                    f"格式={artifact.payload['format']}，模式={artifact.payload['mode']}"
                )
            else:
                continue
            evidence.append(
                EvidenceItem(
                    id=f"ev-{len(evidence) + 1:04d}",
                    subject=subject,
                    fact=fact,
                    source=artifact.source,
                )
            )
        # Runtime source tools and ClaimLedger use stable E-* identifiers. Mapper
        # internals may emit legacy ev-* ids, so normalize once at the boundary.
        evidence = [
            item.model_copy(update={"id": f"E-{index:04d}"})
            for index, item in enumerate(evidence, start=1)
        ]
        state["evidence_items"] = evidence
        state["photo_assets"] = photo_assets
        state["mapping_gaps"] = mapping_gaps
        self.store.write_jsonl(
            "Work/evidence.jsonl",
            [item.model_dump(mode="json") for item in evidence],
        )
        self.store.write_json(
            "Work/photo-manifest.json",
            {"assets": [asset.model_dump(mode="json") for asset in photo_assets]},
        )
        self.store.write_json("Work/mapping-gaps.json", {"gaps": mapping_gaps})
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))

    async def _evaluate_coverage(self, state: dict) -> None:
        request: ReportRequest = state["request"]
        evidence: list[EvidenceItem] = state.get("evidence_items", [])
        coverage = evaluate_coverage(request, evidence)
        state["coverage_matrix"] = coverage
        self.store.write_json("Work/coverage.json", coverage.model_dump(mode="json"))
