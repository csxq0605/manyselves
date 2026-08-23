"""Validated context-only summaries for reporting Agent handoffs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    AgentResult,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import ReportingModel

from .store import ReportingStore


class AgentSessionSummary(ReportingModel):
    summary_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    input_refs: list[Path]
    output_refs: list[Path]
    prior_result_ref: Path | None = None
    message_count: int = Field(ge=0)
    tool_names: list[str]
    agent_rationale: str
    context_only: Literal[True] = True
    # Typed context provenance is an optional reference/hash carrier.  The
    # summary never embeds conversation messages or tool-result prose.
    context_manifest_ref: Path | None = None
    context_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    capsule_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    remaining_work: list[str] = Field(default_factory=list)


class SessionSummarySkeleton(ReportingModel):
    summary_id: str
    run_id: str
    task_id: str
    agent_id: str
    session_id: str
    objective: str
    input_refs: list[Path]
    prior_result_ref: Path | None
    message_count: int
    tool_names: list[str]
    context_manifest_ref: Path | None = None
    context_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    capsule_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    remaining_work: list[str] = Field(default_factory=list)


class SessionSummaryBuilder:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()

    def _validate_refs(self, refs: list[str | Path]) -> list[Path]:
        validated: list[Path] = []
        for ref in dict.fromkeys(Path(item) for item in refs if item):
            target = (self.workspace / ref).resolve()
            if (
                ref.is_absolute()
                or not target.is_relative_to(self.workspace)
                or not target.is_file()
            ):
                raise FileNotFoundError(f"session summary artifact is missing: {ref}")
            validated.append(ref)
        return validated

    def build_skeleton(
        self,
        envelope: TaskEnvelope,
        shared_artifacts: list[str],
        *,
        session_id: str,
        message_log: list[Any],
        context_manifest_ref: str | Path | None = None,
        context_manifest_sha256: str | None = None,
        capsule_sha256: str | None = None,
        remaining_work: list[str] | None = None,
    ) -> SessionSummarySkeleton:
        refs = self._validate_refs(
            [*envelope.input_refs, *envelope.context_summary_refs, *shared_artifacts]
        )
        prior = (
            self._validate_refs([envelope.prior_result_ref]) if envelope.prior_result_ref else []
        )
        tool_names: list[str] = []
        for message in message_log:
            calls = (
                message.get("tool_calls", [])
                if isinstance(message, dict)
                else getattr(message, "tool_calls", [])
            )
            for call in calls or []:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
                if name and name not in tool_names:
                    tool_names.append(name)
        return SessionSummarySkeleton(
            summary_id=f"summary-{uuid4().hex[:12]}",
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            agent_id=envelope.agent_id,
            session_id=session_id,
            objective=envelope.objective,
            input_refs=refs,
            prior_result_ref=prior[0] if prior else None,
            message_count=len(message_log),
            tool_names=tool_names,
            context_manifest_ref=(
                self._validate_refs([context_manifest_ref])[0]
                if context_manifest_ref
                else None
            ),
            context_manifest_sha256=context_manifest_sha256,
            capsule_sha256=capsule_sha256,
            remaining_work=list(dict.fromkeys(str(item) for item in (remaining_work or ()) if str(item))),
        )

    def finalize(
        self,
        skeleton: SessionSummarySkeleton,
        result: AgentResult,
        *,
        output_refs: list[str],
        agent_rationale: str,
    ) -> AgentSessionSummary:
        if result.run_id != skeleton.run_id or result.task_id != skeleton.task_id:
            raise ValueError("session summary result does not match its task skeleton")
        return AgentSessionSummary(
            **skeleton.model_dump(),
            status=result.status.value,
            output_refs=self._validate_refs(output_refs),
            agent_rationale=agent_rationale.strip() or result.reason or "未提供额外 rationale。",
        )


class SessionSummaryStore:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.store = ReportingStore(self.workspace)

    def save(self, summary: AgentSessionSummary) -> Path:
        path = self.store.write_run_model(
            summary.run_id,
            f"session-summaries/{summary.summary_id}.json",
            summary,
        )
        return path.relative_to(self.workspace)

    def load(self, relative: Path) -> AgentSessionSummary:
        path = (self.workspace / relative).resolve()
        if not path.is_relative_to(self.workspace) or not path.is_file():
            raise FileNotFoundError(f"session summary does not exist: {relative}")
        return AgentSessionSummary.model_validate_json(path.read_text(encoding="utf-8"))

    def relevant(
        self, *, run_id: str, agent_id: str | None = None, task_id: str | None = None
    ) -> list[Path]:
        root = self.workspace / "Work/runs" / Path(run_id).name / "session-summaries"
        matches: list[Path] = []
        for path in sorted(root.glob("*.json")):
            summary = AgentSessionSummary.model_validate_json(path.read_text(encoding="utf-8"))
            if agent_id is not None and summary.agent_id != agent_id:
                continue
            if task_id is not None and summary.task_id != task_id:
                continue
            matches.append(path.relative_to(self.workspace))
        return matches
