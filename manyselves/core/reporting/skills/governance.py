"""Per-Skill feedback, evaluation, publication, activation, and rollback."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ConfigDict, Field, model_validator

from manyselves.capabilities.distribution_reporting.domain.taxonomy import resolve_submodule
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import StrictModel

ModuleId = Literal["2.1", "2.2", "2.3", "2.4", "2.5", "all"]
SkillScope = Literal["project", "product"]


class ImmutableSkillModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeedbackRecord(ImmutableSkillModel):
    id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    module_id: ModuleId
    feedback: str = Field(min_length=1)
    explicit_promotion_requested: Literal[True]
    report_version_id: str = Field(min_length=1)
    artifact_refs: list[str] = Field(default_factory=list)
    created_at: datetime


class SkillCandidate(ImmutableSkillModel):
    id: str = Field(min_length=1)
    feedback_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    module_id: ModuleId
    title: str = Field(min_length=1)
    submodules: list[str] = Field(min_length=1)
    proposed_content: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    sample_refs: list[str] = Field(default_factory=list)
    created_at: datetime

    @model_validator(mode="after")
    def submodules_match_module(self) -> "SkillCandidate":
        if self.module_id == "all":
            if self.submodules != ["all"]:
                raise ValueError(
                    "cross-module candidate requires module_id='all' and submodules=['all']"
                )
            return self
        if any(resolve_submodule(item).module_id != self.module_id for item in self.submodules):
            raise ValueError("candidate submodule belongs to another module")
        return self


class EvaluationResult(ImmutableSkillModel):
    id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    module_id: ModuleId
    baseline: float = Field(ge=0.0, le=1.0)
    candidate_score: float = Field(ge=0.0, le=1.0)
    regressions: list[str] = Field(default_factory=list)
    model: str = Field(min_length=1)
    configuration: dict = Field(default_factory=dict)
    created_at: datetime


class SkillVersion(ImmutableSkillModel):
    id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    module_id: ModuleId
    title: str = Field(min_length=1)
    submodules: list[str] = Field(min_length=1)
    sequence: int = Field(ge=1)
    content: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(min_length=1)
    evaluation_id: str = Field(min_length=1)
    scope: SkillScope
    published_at: datetime


class SkillManifest(StrictModel):
    active_versions: dict[str, str] = Field(default_factory=dict)
    activation_history: list[dict] = Field(default_factory=list)


class SkillGovernanceStore:
    """Persist immutable artifacts beneath one explicit project or product root."""

    def __init__(self, root: Path, *, scope: SkillScope):
        self.root = Path(root).resolve()
        self.scope = scope
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid4().hex[:12]}"

    @staticmethod
    def _safe_skill_id(skill_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", skill_id or ""):
            raise ValueError("skill_id must use letters, digits, dots, dashes, or underscores")
        return skill_id

    def _write_new(self, path: Path, value: StrictModel) -> None:
        if path.exists():
            raise ValueError(f"governance artifact already exists: {path.name}")
        self._write(path, value)

    def _write(self, path: Path, value: StrictModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _read(path: Path, model_type):
        if not path.is_file():
            raise ValueError(f"governance artifact not found: {path.name}")
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))

    @property
    def _manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def _manifest(self) -> SkillManifest:
        if not self._manifest_path.is_file():
            return SkillManifest()
        return SkillManifest.model_validate_json(self._manifest_path.read_text(encoding="utf-8"))

    def record_feedback(
        self,
        *,
        skill_id: str,
        module_id: ModuleId,
        feedback: str,
        explicit_promotion_requested: bool,
        report_version_id: str,
        artifact_refs: list[str] | None = None,
    ) -> FeedbackRecord:
        if not explicit_promotion_requested:
            raise ValueError("explicit Skill promotion intent is required")
        record = FeedbackRecord(
            id=self._new_id("feedback"),
            skill_id=self._safe_skill_id(skill_id),
            module_id=module_id,
            feedback=feedback,
            explicit_promotion_requested=True,
            report_version_id=report_version_id,
            artifact_refs=artifact_refs or [],
            created_at=self._now(),
        )
        with self._lock:
            self._write_new(self.root / "feedback" / f"{record.id}.json", record)
        return record

    def create_candidate(
        self,
        feedback_id: str,
        *,
        title: str,
        submodules: list[str],
        proposed_content: str,
        reason: str,
        sample_refs: list[str] | None = None,
    ) -> SkillCandidate:
        feedback = self._read(self.root / "feedback" / f"{feedback_id}.json", FeedbackRecord)
        candidate = SkillCandidate(
            id=self._new_id("candidate"),
            feedback_id=feedback.id,
            skill_id=feedback.skill_id,
            module_id=feedback.module_id,
            title=title,
            submodules=submodules,
            proposed_content=proposed_content,
            reason=reason,
            sample_refs=sample_refs or [],
            created_at=self._now(),
        )
        self.save_candidate(candidate)
        return candidate

    def save_candidate(self, candidate: SkillCandidate) -> SkillCandidate:
        with self._lock:
            self._write_new(self.root / "candidates" / f"{candidate.id}.json", candidate)
        return candidate

    def record_evaluation(
        self,
        candidate_id: str,
        *,
        baseline: float,
        candidate_score: float,
        regressions: list[str],
        model: str,
        configuration: dict | None = None,
    ) -> EvaluationResult:
        candidate = self._read(self.root / "candidates" / f"{candidate_id}.json", SkillCandidate)
        evaluation = EvaluationResult(
            id=self._new_id("evaluation"),
            candidate_id=candidate.id,
            skill_id=candidate.skill_id,
            module_id=candidate.module_id,
            baseline=baseline,
            candidate_score=candidate_score,
            regressions=regressions,
            model=model,
            configuration=configuration or {},
            created_at=self._now(),
        )
        with self._lock:
            self._write_new(self.root / "evaluations" / f"{evaluation.id}.json", evaluation)
        return evaluation

    def publish(self, candidate_id: str, evaluation_id: str, *, confirmed: bool) -> SkillVersion:
        if not confirmed:
            raise ValueError("explicit user confirmation is required to publish a Skill")
        with self._lock:
            candidate = self._read(
                self.root / "candidates" / f"{candidate_id}.json", SkillCandidate
            )
            evaluation = self._read(
                self.root / "evaluations" / f"{evaluation_id}.json", EvaluationResult
            )
            if evaluation.candidate_id != candidate.id or evaluation.skill_id != candidate.skill_id:
                raise ValueError("candidate and evaluation do not belong together")
            if evaluation.candidate_score < evaluation.baseline or evaluation.regressions:
                raise ValueError("evaluation regresses against the approved baseline")
            version_dir = self.root / "versions" / self._safe_skill_id(candidate.skill_id)
            sequence = len(list(version_dir.glob("*.json"))) + 1 if version_dir.is_dir() else 1
            version = SkillVersion(
                id=f"{candidate.skill_id}-v{sequence:04d}",
                skill_id=candidate.skill_id,
                module_id=candidate.module_id,
                title=candidate.title,
                submodules=candidate.submodules,
                sequence=sequence,
                content=candidate.proposed_content,
                content_sha256=hashlib.sha256(
                    candidate.proposed_content.encode("utf-8")
                ).hexdigest(),
                candidate_id=candidate.id,
                evaluation_id=evaluation.id,
                scope=self.scope,
                published_at=self._now(),
            )
            self._write_new(version_dir / f"{version.id}.json", version)
            self._activate(version, action="publish")
            return version

    def _activate(self, version: SkillVersion, *, action: str) -> None:
        manifest = self._manifest()
        manifest.active_versions[version.skill_id] = version.id
        manifest.activation_history.append(
            {
                "skill_id": version.skill_id,
                "version_id": version.id,
                "action": action,
                "at": self._now().isoformat(),
            }
        )
        self._write(self._manifest_path, manifest)

    def rollback(self, *, skill_id: str, version_id: str) -> SkillVersion:
        with self._lock:
            version = self._read(
                self.root / "versions" / self._safe_skill_id(skill_id) / f"{version_id}.json",
                SkillVersion,
            )
            if version.skill_id != skill_id:
                raise ValueError("rollback version belongs to another Skill")
            self._activate(version, action="rollback")
            return version

    def active_version(self, skill_id: str) -> SkillVersion | None:
        version_id = self._manifest().active_versions.get(skill_id)
        if version_id is None:
            return None
        return self._read(
            self.root / "versions" / self._safe_skill_id(skill_id) / f"{version_id}.json",
            SkillVersion,
        )

    def active_versions(self) -> list[SkillVersion]:
        return [
            version
            for skill_id in sorted(self._manifest().active_versions)
            if (version := self.active_version(skill_id)) is not None
        ]

    def load_version(self, version_id: str) -> SkillVersion:
        matches = list((self.root / "versions").glob(f"*/{version_id}.json"))
        if len(matches) != 1:
            raise ValueError(f"Skill version not found or ambiguous: {version_id}")
        return self._read(matches[0], SkillVersion)
