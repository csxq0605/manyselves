"""Capability-owned access to one already persisted Agent completion.

The durable attempt store remains the owner of current-attempt lookup and
result integrity checks.  This module only supplies the Capability adapter
that compares the existing task identity, decodes the existing AgentResult,
and exposes a completed payload to the neutral Agent bridge.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    AgentResult,
    TaskEnvelope,
)

from .state.parallel import (
    IdentityLeaseHandle,
    IdentityLeaseManager,
    TaskAttemptStore,
    TaskCorrelation,
)

if TYPE_CHECKING:
    from .state.parallel import IdentityLease


def build_task_correlation(
    workspace: Path,
    envelope: TaskEnvelope,
    *,
    workflow_id: str,
    identity_key: str,
    session_id: str,
    identity_lease: "IdentityLease",
    execution_profile_sha256: str = "0" * 64,
) -> TaskCorrelation:
    """Build the existing Reporting task correlation without changing it.

    The body is the mechanical extraction of the old
    ``ReportingAgentRunner._task_correlation`` helper.  The Capability caller
    supplies the resolved execution-profile digest and live identity lease;
    this function does not invent either one.
    """

    workspace = Path(workspace).resolve()
    input_contract_ref = envelope.input_contract_ref
    input_contract_sha256: str | None = None
    subject_ref: str | None = None
    subject_sha256: str | None = None
    if input_contract_ref:
        contract_path = (workspace / input_contract_ref).resolve()
        if not (
            contract_path.is_relative_to(workspace)
            and contract_path.is_file()
        ):
            raise ValueError("task input contract is not a readable workspace artifact")
        contract_bytes = contract_path.read_bytes()
        input_contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
        try:
            contract_payload = json.loads(contract_bytes)
        except (TypeError, ValueError) as exc:
            raise ValueError("task input contract is not valid JSON") from exc
        candidate_ref = next(
            (
                contract_payload.get(field)
                for field in ("subject_ref", "base_subject_ref")
                if isinstance(contract_payload.get(field), str)
            ),
            None,
        )
        if candidate_ref:
            candidate_path = (workspace / candidate_ref).resolve()
            if not (
                candidate_path.is_relative_to(workspace)
                and candidate_path.is_file()
            ):
                raise ValueError("task subject is not a readable workspace artifact")
            subject_ref = candidate_ref
            subject_sha256 = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    task_envelope_sha256 = hashlib.sha256(
        json.dumps(
            envelope.model_dump(
                mode="json",
                exclude={"task_attempt_id"},
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return TaskCorrelation(
        workflow_id=workflow_id,
        run_id=envelope.run_id,
        task_id=envelope.task_id,
        task_attempt_id=envelope.task_attempt_id,
        agent_id=envelope.agent_id,
        identity_key=identity_key,
        session_id=session_id,
        task_envelope_sha256=task_envelope_sha256,
        execution_profile_sha256=execution_profile_sha256,
        input_contract_ref=input_contract_ref,
        input_contract_sha256=input_contract_sha256,
        subject_ref=subject_ref,
        subject_sha256=subject_sha256,
        lease_owner_id=identity_lease.owner_id,
        lease_epoch=identity_lease.lease_epoch,
    )


def same_recoverable_task(
    previous: TaskCorrelation,
    current: TaskCorrelation,
) -> bool:
    """Compare the existing durable recovery identity exactly as before.

    Attempt and lease fields are intentionally excluded because the existing
    Runner uses this comparison to find a reusable semantic task across a
    later physical attempt or lease.  No new identity or integrity policy is
    introduced here.
    """

    excluded = {"task_attempt_id", "lease_owner_id", "lease_epoch"}
    return previous.model_dump(exclude=excluded) == current.model_dump(
        exclude=excluded
    )


def reporting_identity_key(agent_id: str, session_key: str | None) -> str:
    """Project the existing durable Reporting identity for one Agent task."""

    if agent_id == "evidence-auditor" and session_key:
        return session_key
    if (
        agent_id == "cross-module-reviewer"
        and session_key
        and session_key.startswith("cross-owner-")
    ):
        return session_key
    if agent_id in {"chief-editor", "chief-editor-auditor"} and session_key:
        for prefix in ("chief-chapter-", "final-chapter-"):
            if session_key.startswith(prefix):
                return session_key.split("-r", 1)[0]
    return agent_id


def load_completed_agent_result(
    workspace: Path,
    expected: TaskCorrelation,
) -> AgentResult | None:
    """Load a matching completed Agent result through the existing store.

    ``TaskAttemptStore`` remains the sole owner of persisted terminal/result
    validation, including its existing hash/CAS-related behavior.  The
    expected correlation must be supplied by the Capability binding; this
    helper does not synthesize lease, attempt, or digest values.
    """

    store = TaskAttemptStore(Path(workspace), expected.run_id)
    current = store.current(expected.task_id)
    if current is None or not same_recoverable_task(current, expected):
        return None
    recovered = store.load_verified_attempt(
        current.task_id,
        current.task_attempt_id,
    )
    if recovered is None:
        return None
    terminal, payload = recovered
    if not same_recoverable_task(terminal.correlation, expected):
        raise RuntimeError("persisted task attempt identity or hash mismatch")
    result = AgentResult.model_validate(payload)
    if terminal.status != result.status.value:
        raise RuntimeError("persisted task result status does not match its terminal")
    if (
        result.task_id != expected.task_id
        or result.run_id != expected.run_id
        or result.agent_id != expected.agent_id
        or result.session_id != expected.session_id
    ):
        raise RuntimeError("persisted task result does not match its Agent identity")
    if terminal.status != "completed":
        return None
    return result


@dataclass(slots=True)
class ProviderTaskAttempt:
    """One live use of the existing Reporting task-attempt protocol.

    The ordering is the mechanically retained behavior: acquire the existing
    identity lease, compare/load the prior completed result, and activate the
    current physical attempt only when that semantic result is not reusable.
    """

    workspace: Path
    correlation: TaskCorrelation
    lease_handle: IdentityLeaseHandle
    _activated: bool = False

    @classmethod
    def acquire(
        cls,
        workspace: Path,
        envelope: TaskEnvelope,
        *,
        workflow_id: str,
        identity_key: str,
        session_id: str,
        execution_profile_sha256: str = "0" * 64,
    ) -> "ProviderTaskAttempt":
        workspace = Path(workspace).resolve()
        manager = IdentityLeaseManager(workspace, envelope.run_id)
        handle = manager.acquire(
            workflow_id,
            identity_key,
            owner_id=(
                f"{IdentityLeaseManager.default_owner_id()}:"
                f"{envelope.task_attempt_id}"
            ),
        )
        try:
            correlation = build_task_correlation(
                workspace,
                envelope,
                workflow_id=workflow_id,
                identity_key=identity_key,
                session_id=session_id,
                identity_lease=handle.lease,
                execution_profile_sha256=execution_profile_sha256,
            )
            store = TaskAttemptStore(workspace, envelope.run_id)
            previous = store.current(envelope.task_id)
            if (
                previous is not None
                and previous.task_attempt_id == correlation.task_attempt_id
                and same_recoverable_task(previous, correlation)
            ):
                recovered = store.load_verified_attempt(
                    previous.task_id,
                    previous.task_attempt_id,
                )
                if (
                    recovered is not None
                    and not same_recoverable_task(
                        recovered[0].correlation,
                        correlation,
                    )
                ):
                    raise RuntimeError(
                        "persisted task attempt identity or hash mismatch"
                    )
                if recovered is not None and recovered[0].status != "completed":
                    # Keep the semantic task and session, but give the resumed
                    # dispatch its own append-only result path.
                    correlation = correlation.model_copy(
                        update={"task_attempt_id": f"attempt-{uuid4().hex}"}
                    )
        except Exception:
            handle.release()
            raise
        return cls(workspace, correlation, handle)

    def activate(self) -> None:
        if self._activated:
            return
        TaskAttemptStore(self.workspace, self.correlation.run_id).activate(
            self.correlation
        )
        self._activated = True

    def load_completed_or_activate(self) -> AgentResult | None:
        completed = load_completed_agent_result(self.workspace, self.correlation)
        if completed is None:
            self.activate()
        return completed

    def close(self) -> None:
        self.lease_handle.release()

    def __enter__(self) -> "ProviderTaskAttempt":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = [
    "ProviderTaskAttempt",
    "build_task_correlation",
    "load_completed_agent_result",
    "reporting_identity_key",
    "same_recoverable_task",
]
