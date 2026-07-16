"""Persistent candidate, evaluation, publication, and rollback governance."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from ..agentic_models import StrictModel

ModuleId = Literal["2.1", "2.2", "2.3", "2.4", "2.5"]


class SkillCandidate(StrictModel):
    id: str = Field(min_length=1)
    module_id: ModuleId
    original: str = Field(min_length=1)
    revised: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    failure_type: str | None = None
    sample_refs: list[str] = Field(default_factory=list)
    created_at: datetime


class EvaluationResult(StrictModel):
    id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    module_id: ModuleId
    baseline: float = Field(ge=0.0, le=1.0)
    candidate_score: float = Field(ge=0.0, le=1.0)
    regressions: list[str] = Field(default_factory=list)
    model: str = Field(min_length=1)
    configuration: dict = Field(default_factory=dict)
    created_at: datetime


class SkillVersion(StrictModel):
    id: str = Field(min_length=1)
    module_id: ModuleId
    sequence: int = Field(ge=1)
    content: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    evaluation_id: str = Field(min_length=1)
    published_at: datetime


class SkillManifest(StrictModel):
    active_versions: dict[str, str] = Field(default_factory=dict)
    activation_history: list[dict] = Field(default_factory=list)


class SkillGovernanceStore:
    """Keep all learning artifacts in ``Capabilities/skills`` under the project."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "Capabilities" / "skills"
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid4().hex[:12]}"

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

    def create_candidate(
        self,
        *,
        module_id: ModuleId,
        original: str,
        revised: str,
        reason: str,
        failure_type: str | None = None,
        sample_refs: list[str] | None = None,
    ) -> SkillCandidate:
        candidate = SkillCandidate(
            id=self._new_id("candidate"),
            module_id=module_id,
            original=original,
            revised=revised,
            reason=reason,
            failure_type=failure_type,
            sample_refs=sample_refs or [],
            created_at=self._now(),
        )
        with self._lock:
            self._write(self.root / "candidates" / f"{candidate.id}.json", candidate)
        return candidate

    def record_evaluation(
        self,
        candidate_id: str,
        *,
        baseline: float,
        candidate_score: float,
        regressions: list[str],
        model: str = "unspecified",
        configuration: dict | None = None,
    ) -> EvaluationResult:
        with self._lock:
            candidate = self._read(
                self.root / "candidates" / f"{candidate_id}.json", SkillCandidate
            )
            evaluation = EvaluationResult(
                id=self._new_id("evaluation"),
                candidate_id=candidate.id,
                module_id=candidate.module_id,
                baseline=baseline,
                candidate_score=candidate_score,
                regressions=regressions,
                model=model,
                configuration=configuration or {},
                created_at=self._now(),
            )
            self._write(self.root / "evaluations" / f"{evaluation.id}.json", evaluation)
        return evaluation

    def publish(
        self,
        candidate_id: str,
        evaluation_id: str,
        *,
        confirmed: bool,
    ) -> SkillVersion:
        if not confirmed:
            raise ValueError("explicit user confirmation is required to publish a Skill")
        with self._lock:
            candidate = self._read(
                self.root / "candidates" / f"{candidate_id}.json", SkillCandidate
            )
            evaluation = self._read(
                self.root / "evaluations" / f"{evaluation_id}.json",
                EvaluationResult,
            )
            if (
                evaluation.candidate_id != candidate.id
                or evaluation.module_id != candidate.module_id
            ):
                raise ValueError("candidate and evaluation do not belong together")
            if evaluation.candidate_score < evaluation.baseline or evaluation.regressions:
                raise ValueError("evaluation regresses against the approved baseline")
            version_dir = self.root / "versions" / candidate.module_id
            existing = list(version_dir.glob("*.json")) if version_dir.is_dir() else []
            sequence = len(existing) + 1
            version = SkillVersion(
                id=f"skill-{candidate.module_id.replace('.', '-')}-v{sequence:04d}",
                module_id=candidate.module_id,
                sequence=sequence,
                content=candidate.revised,
                candidate_id=candidate.id,
                evaluation_id=evaluation.id,
                published_at=self._now(),
            )
            self._write(version_dir / f"{version.id}.json", version)
            self._activate(version, action="publish")
            return version

    def _activate(self, version: SkillVersion, *, action: str) -> None:
        manifest = self._manifest()
        manifest.active_versions[version.module_id] = version.id
        manifest.activation_history.append(
            {
                "module_id": version.module_id,
                "version_id": version.id,
                "action": action,
                "at": self._now().isoformat(),
            }
        )
        self._write(self._manifest_path, manifest)

    def rollback(self, *, module_id: ModuleId, version_id: str) -> SkillVersion:
        with self._lock:
            version = self._read(
                self.root / "versions" / module_id / f"{version_id}.json",
                SkillVersion,
            )
            if version.module_id != module_id:
                raise ValueError("rollback version belongs to another module")
            self._activate(version, action="rollback")
            return version

    def active_version(self, module_id: ModuleId) -> SkillVersion | None:
        with self._lock:
            version_id = self._manifest().active_versions.get(module_id)
            if version_id is None:
                return None
            return self._read(
                self.root / "versions" / module_id / f"{version_id}.json",
                SkillVersion,
            )
