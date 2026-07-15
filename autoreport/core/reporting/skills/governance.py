"""Immutable Skill candidate regression, publication, and rollback records."""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..models import ReportingModel

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class SkillPublishError(ValueError):
    pass


class RegressionCase(ReportingModel):
    id: str
    prompt: str
    required_phrases: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)


class SkillCandidate(ReportingModel):
    id: str
    skill_id: str
    base_version: str
    candidate_version: str
    content: str
    created_at: str


class RegressionCaseResult(ReportingModel):
    case_id: str
    passed: bool
    output: str
    failures: list[str]


class RegressionEvaluation(ReportingModel):
    candidate_id: str
    passed: bool
    results: list[RegressionCaseResult]
    evaluated_at: str


class SkillVersionRecord(ReportingModel):
    skill_id: str
    version: str
    action: Literal["baseline", "publish", "rollback"]
    candidate_id: str | None = None
    reason: str | None = None
    created_at: str


RegressionRunner = Callable[[SkillCandidate, RegressionCase], str]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _version_tuple(version: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(version)
    if not match:
        raise SkillPublishError(f"invalid semantic version: {version}")
    return tuple(int(part) for part in match.groups())


class SkillGovernance:
    def __init__(self, root: Path):
        self.root = Path(root)
        for name in ("candidates", "evaluations", "versions", "active", "history"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def _write_json(self, path: Path, value: ReportingModel | dict) -> None:
        payload = value.model_dump(mode="json") if isinstance(value, ReportingModel) else value
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _version_path(self, skill_id: str, version: str) -> Path:
        return self.root / "versions" / skill_id / f"{version}.md"

    def _candidate_path(self, skill_id: str, version: str) -> Path:
        return self.root / "candidates" / skill_id / f"{version}.json"

    def _append_history(self, record: SkillVersionRecord) -> None:
        path = self.root / "history" / f"{record.skill_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(record.model_dump_json() + "\n")

    def register_baseline(
        self,
        skill_id: str,
        version: str,
        content: str,
    ) -> SkillVersionRecord:
        _version_tuple(version)
        path = self._version_path(skill_id, version)
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                raise SkillPublishError(f"version already exists: {skill_id}@{version}")
            records = self.history(skill_id)
            return next(record for record in records if record.version == version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        record = SkillVersionRecord(
            skill_id=skill_id,
            version=version,
            action="baseline",
            created_at=_now(),
        )
        self._write_json(self.root / "active" / f"{skill_id}.json", record)
        self._append_history(record)
        return record

    def create_candidate(
        self,
        skill_id: str,
        base_version: str,
        candidate_version: str,
        content: str,
    ) -> SkillCandidate:
        if _version_tuple(candidate_version) <= _version_tuple(base_version):
            raise SkillPublishError("candidate version must be greater than base version")
        if self.active_version(skill_id) != base_version:
            raise SkillPublishError(f"base version is not active: {skill_id}@{base_version}")
        path = self._candidate_path(skill_id, candidate_version)
        if path.exists():
            raise SkillPublishError(f"candidate already exists: {skill_id}@{candidate_version}")
        candidate = SkillCandidate(
            id=f"{skill_id}@{candidate_version}",
            skill_id=skill_id,
            base_version=base_version,
            candidate_version=candidate_version,
            content=content,
            created_at=_now(),
        )
        self._write_json(path, candidate)
        return candidate

    def evaluate(
        self,
        candidate: SkillCandidate,
        cases: list[RegressionCase],
        runner: RegressionRunner,
    ) -> RegressionEvaluation:
        results = []
        for case in cases:
            output = runner(candidate, case)
            failures = [
                f"missing required phrase: {phrase}"
                for phrase in case.required_phrases
                if phrase not in output
            ]
            failures.extend(
                f"contains forbidden phrase: {phrase}"
                for phrase in case.forbidden_phrases
                if phrase in output
            )
            results.append(
                RegressionCaseResult(
                    case_id=case.id,
                    passed=not failures,
                    output=output,
                    failures=failures,
                )
            )
        evaluation = RegressionEvaluation(
            candidate_id=candidate.id,
            passed=bool(results) and all(result.passed for result in results),
            results=results,
            evaluated_at=_now(),
        )
        self._write_json(
            self.root / "evaluations" / candidate.skill_id / f"{candidate.candidate_version}.json",
            evaluation,
        )
        return evaluation

    def publish(
        self,
        candidate: SkillCandidate,
        evaluation: RegressionEvaluation,
    ) -> SkillVersionRecord:
        if evaluation.candidate_id != candidate.id or not evaluation.passed:
            raise SkillPublishError("candidate regression did not pass")
        if self.active_version(candidate.skill_id) != candidate.base_version:
            raise SkillPublishError("candidate baseline is stale")
        path = self._version_path(candidate.skill_id, candidate.candidate_version)
        if path.exists():
            raise SkillPublishError(f"version already exists: {candidate.id}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(candidate.content, encoding="utf-8")
        record = SkillVersionRecord(
            skill_id=candidate.skill_id,
            version=candidate.candidate_version,
            action="publish",
            candidate_id=candidate.id,
            created_at=_now(),
        )
        self._write_json(self.root / "active" / f"{candidate.skill_id}.json", record)
        self._append_history(record)
        return record

    def rollback(self, skill_id: str, version: str, *, reason: str) -> SkillVersionRecord:
        if not self._version_path(skill_id, version).is_file():
            raise SkillPublishError(f"unknown rollback version: {skill_id}@{version}")
        record = SkillVersionRecord(
            skill_id=skill_id,
            version=version,
            action="rollback",
            reason=reason,
            created_at=_now(),
        )
        self._write_json(self.root / "active" / f"{skill_id}.json", record)
        self._append_history(record)
        return record

    def active_version(self, skill_id: str) -> str | None:
        path = self.root / "active" / f"{skill_id}.json"
        if not path.is_file():
            return None
        return str(json.loads(path.read_text(encoding="utf-8"))["version"])

    def history(self, skill_id: str) -> list[SkillVersionRecord]:
        path = self.root / "history" / f"{skill_id}.jsonl"
        if not path.is_file():
            return []
        return [
            SkillVersionRecord.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
