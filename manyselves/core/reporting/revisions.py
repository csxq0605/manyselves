"""Version-aware coordination for post-delivery report feedback."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .agent_runner import ReportingAgentRunner
from .agentic_models import (
    EditedReportSubmission,
    ModuleDispatchPlan,
    ModuleSubmission,
    TaskEnvelope,
)
from .models import (
    REPORT_MODULE_IDS,
    EvidenceItem,
    OutputArtifact,
    PhotoAsset,
    ReportRequest,
    RevisionRequest,
)
from .output_verifier import OutputVerificationError, verify_current_run_outputs
from .session_summary import AgentSessionSummary
from .skills.service import ProjectSkillEvolutionService
from .versions import ReportVersion, ReportVersionStore
from .workflow import (
    ReportingNeedsDecisionError,
    ReportWorkflowRunner,
    ScopeExpansionNeededError,
)

if TYPE_CHECKING:
    from .service import ReportingRunResult, ReportingService


class RevisionCoordinator:
    """Restore a parent version and run only the authorized local revision path."""

    def __init__(
        self,
        service: "ReportingService",
        agent_runner: ReportingAgentRunner,
    ):
        self.service = service
        self.agent_runner = agent_runner

    async def run(
        self,
        request: RevisionRequest,
        *,
        run_id: str | None = None,
        resume: bool = False,
    ) -> "ReportingRunResult":
        from .service import ReportingRunResult

        self.service.store.ensure_layout()
        run_id = run_id or f"report-revision-{uuid.uuid4().hex[:10]}"
        execution_started_ns = time.time_ns()
        self.service.store.write_json(
            f"Work/runs/{run_id}/revision-request.json",
            request.model_dump(mode="json"),
        )
        try:
            baseline = ReportVersionStore(self.service.workspace).load(request.baseline_version_id)
            state, baseline_edited = self._restore(run_id, request, baseline)
            state["resume"] = resume
            await ReportWorkflowRunner(self.service, self.agent_runner).run_revision(
                state, request, baseline_edited
            )
        except asyncio.CancelledError:
            result = ReportingRunResult(
                run_id=run_id,
                status="cancelled",
                error="interrupted by user",
            )
            self.service._save_run(result)
            await self.service._notice("用户已中断报告修订；当前进度已保存。")
            return result
        except ScopeExpansionNeededError as exc:
            expansion = exc.request
            self.service.store.write_json(
                f"Work/runs/{run_id}/scope-expansions/{expansion.request_id}.json",
                expansion.model_dump(mode="json"),
            )
            result = ReportingRunResult(
                run_id=run_id,
                status="needs_scope_expansion",
                scope_expansion_request_id=expansion.request_id,
                error=expansion.reason,
            )
            self.service._save_run(result)
            await self.service._notice(
                "局部修订发现范围外变化，已形成 scope expansion request 等待确认。"
            )
            return result
        except ReportingNeedsDecisionError as exc:
            result = ReportingRunResult(
                run_id=run_id,
                status="needs_decision",
                error=str(exc),
            )
            self.service._save_run(result)
            return result
        except Exception as exc:
            result = ReportingRunResult(run_id=run_id, status="failed", error=str(exc))
            self.service._save_run(result)
            return result

        artifacts: list[OutputArtifact] = state.get("output_artifacts", [])
        feedback_record_id: str | None = None
        if request.promote_to_skill:
            feedback_record = ProjectSkillEvolutionService(self.service.workspace).record_feedback(
                skill_id=request.promote_skill_id or "",
                module_id=request.target_module_ids[0],
                feedback=request.feedback,
                explicit_promotion_requested=True,
                report_version_id=run_id,
                artifact_refs=[f"Work/runs/{run_id}/revision-request.json"],
            )
            feedback_record_id = feedback_record.id
        try:
            output_paths = verify_current_run_outputs(
                self.service.workspace, run_id, artifacts, execution_started_ns
            )
        except OutputVerificationError as exc:
            result = ReportingRunResult(run_id=run_id, status="failed", error=str(exc))
            self.service._save_run(result)
            return result
        result = ReportingRunResult(
            run_id=run_id,
            status="completed",
            output_paths=output_paths,
            feedback_record_id=feedback_record_id,
        )
        self.service._save_run(result)
        return result

    def _restore(
        self,
        run_id: str,
        revision: RevisionRequest,
        baseline: ReportVersion,
    ) -> tuple[dict, EditedReportSubmission]:
        refs = baseline.artifact_refs
        missing_keys = [
            key
            for key in [
                *(f"module_submission:{module_id}" for module_id in REPORT_MODULE_IDS),
                "edited_submission",
                "report_request",
                "evidence",
                "source_ledger",
            ]
            if key not in refs
        ]
        if missing_keys:
            raise ValueError(f"baseline version lacks restorable artifacts: {missing_keys}")

        modules = {
            module_id: ModuleSubmission.model_validate_json(
                self._read(refs[f"module_submission:{module_id}"])
            )
            for module_id in REPORT_MODULE_IDS
        }
        baseline_edited = EditedReportSubmission.model_validate_json(
            self._read(refs["edited_submission"])
        )
        baseline_request = ReportRequest.model_validate_json(self._read(refs["report_request"]))
        report_request = baseline_request.model_copy(
            update={
                "instruction": revision.feedback,
                "target_modules": list(REPORT_MODULE_IDS),
                "user_supplements": [
                    *baseline_request.user_supplements,
                    *revision.user_supplements,
                ],
            }
        )
        report_request = ReportRequest.model_validate(
            report_request.model_dump(mode="json")
        )
        evidence = [
            EvidenceItem.model_validate_json(line)
            for line in self._read(refs["evidence"]).splitlines()
            if line.strip()
        ]
        evidence_ref = f"Work/runs/{run_id}/evidence.jsonl"
        evidence_path = self.service.store.write_jsonl(
            evidence_ref,
            [item.model_dump(mode="json") for item in evidence],
        )
        shutil.copyfile(evidence_path, self.service.workspace / "Work/evidence.jsonl")
        source_records = json.loads(self._read(refs["source_ledger"]))
        self.service.store.write_json(f"Work/runs/{run_id}/ledgers/sources.json", source_records)

        photo_assets: list[PhotoAsset] = []
        if "photo_manifest" in refs:
            manifest = json.loads(self._read(refs["photo_manifest"]))
            for raw in manifest.get("assets", []):
                asset_id = raw["id"]
                key = f"photo_asset:{asset_id}"
                if key not in refs:
                    raise ValueError(f"baseline photo artifact is missing: {asset_id}")
                photo_assets.append(PhotoAsset.model_validate({**raw, "path": refs[key]}))
        photo_manifest = {
            "assets": [
                asset.model_dump(mode="json")
                for asset in photo_assets
            ]
        }
        photo_manifest_ref = (
            f"Work/runs/{run_id}/photo-manifest.json"
        )
        self.service.store.write_json(
            photo_manifest_ref,
            photo_manifest,
        )
        self.service.store.write_json(
            "Work/photo-manifest.json",
            photo_manifest,
        )

        context_by_agent: dict[str, list[str]] = {}
        for relative in baseline.session_summary_refs:
            summary = AgentSessionSummary.model_validate_json(self._read(relative))
            context_key = summary.agent_id
            if summary.agent_id == "evidence-auditor":
                match = re.search(r"2\.[1-5]", summary.task_id)
                if match is not None:
                    context_key = f"evidence-auditor:{match.group(0)}"
            context_by_agent.setdefault(context_key, []).append(relative.as_posix())

        dispatch = ModuleDispatchPlan(
            module_tasks=[
                TaskEnvelope(
                    task_id=f"planned-{module_id}",
                    run_id=run_id,
                    agent_id=f"module-{module_id}-specialist",
                    objective=f"必要时修订模块 {module_id}",
                )
                for module_id in REPORT_MODULE_IDS
            ],
            rationale="从父版本恢复，仅启动获授权的局部修订。",
        )
        return (
            {
                "run_id": run_id,
                "request": report_request,
                "revision_request": revision,
                "parent_version_id": baseline.version_id,
                "module_submissions": modules,
                "baseline_module_refs": {
                    module_id: refs[f"module_submission:{module_id}"].as_posix()
                    for module_id in REPORT_MODULE_IDS
                },
                "module_dispatch": dispatch,
                "evidence_items": evidence,
                "photo_assets": photo_assets,
                "preparation_refs": {
                    "evidence": evidence_ref,
                    "photo_manifest": photo_manifest_ref,
                },
                "revision_context_by_agent": context_by_agent,
                "inherited_summary_refs": list(baseline.session_summary_refs),
            },
            baseline_edited,
        )

    def _read(self, relative: Path) -> str:
        path = (self.service.workspace / relative).resolve()
        if not path.is_relative_to(self.service.workspace) or not path.is_file():
            raise FileNotFoundError(f"version artifact is missing: {relative}")
        return path.read_text(encoding="utf-8")
