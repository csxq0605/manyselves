"""Characterization for same-run Provider task attempt recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.completed_result_recovery import (
    ProviderTaskAttempt,
    build_task_correlation,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    AgentResult,
    AgentRunStatus,
    ModuleRevisionSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
    IdentityLeaseManager,
    TaskAttemptStore,
)


def _result(
    *,
    run_id: str,
    session_id: str,
    status: AgentRunStatus,
) -> AgentResult:
    return AgentResult(
        task_id="module-revision-r1-2.4",
        run_id=run_id,
        agent_id="module-2.4-specialist",
        session_id=session_id,
        status=status,
        payload=(
            ModuleRevisionSubmission(
                module_id="2.4",
                base_revision=0,
                revision=1,
                submodule_narratives={"2.4.1.1": "修订后的正文。"},
                source_ids=[],
            )
            if status == AgentRunStatus.COMPLETED
            else None
        ),
        reason="previous attempt failed" if status == AgentRunStatus.FAILED else None,
    )


def test_identity_lease_rejects_a_second_active_owner(tmp_path: Path) -> None:
    manager = IdentityLeaseManager(tmp_path, "identity-contention")
    first = manager.acquire("public-reporting", "module-2.4-specialist")
    try:
        with pytest.raises(RuntimeError, match="already active"):
            manager.acquire("public-reporting", "module-2.4-specialist")
    finally:
        first.release()

    resumed = manager.acquire("public-reporting", "module-2.4-specialist")
    try:
        assert resumed.lease.lease_epoch == first.lease.lease_epoch + 1
    finally:
        resumed.release()


def test_failed_terminal_resumes_as_new_attempt_in_same_session(tmp_path: Path) -> None:
    """A failed physical attempt must not own the resumed result path."""

    run_id = "same-run-failed-attempt"
    session_id = "public-reporting:module-2.4"
    envelope = TaskEnvelope(
        task_id="module-revision-r1-2.4",
        task_attempt_id="attempt-failed",
        run_id=run_id,
        agent_id="module-2.4-specialist",
        objective="Revise module 2.4.",
    )
    store = TaskAttemptStore(tmp_path, run_id)

    failed = ProviderTaskAttempt.acquire(
        tmp_path,
        envelope,
        workflow_id="public-reporting",
        identity_key=envelope.agent_id,
        session_id=session_id,
    )
    try:
        failed.activate()
        store.persist_result(
            failed.correlation,
            _result(
                run_id=run_id,
                session_id=session_id,
                status=AgentRunStatus.FAILED,
            ).model_dump(mode="json"),
            status="failed",
        )
    finally:
        failed.close()

    resumed = ProviderTaskAttempt.acquire(
        tmp_path,
        envelope,
        workflow_id="public-reporting",
        identity_key=envelope.agent_id,
        session_id=session_id,
    )
    try:
        assert resumed.correlation.task_attempt_id != failed.correlation.task_attempt_id
        assert resumed.correlation.run_id == failed.correlation.run_id
        assert resumed.correlation.task_id == failed.correlation.task_id
        assert resumed.correlation.agent_id == failed.correlation.agent_id
        assert resumed.correlation.session_id == failed.correlation.session_id
        assert resumed.load_completed_or_activate() is None
        assert store.current(envelope.task_id) == resumed.correlation

        store.persist_result(
            resumed.correlation,
            _result(
                run_id=run_id,
                session_id=session_id,
                status=AgentRunStatus.COMPLETED,
            ).model_dump(mode="json"),
            status="completed",
        )
    finally:
        resumed.close()

    old_terminal, old_payload = store.load_verified_result(failed.correlation) or (
        None,
        None,
    )
    new_terminal, new_payload = store.load_verified_result(resumed.correlation) or (
        None,
        None,
    )
    assert old_terminal is not None and old_terminal.status == "failed"
    assert old_payload is not None and old_payload["status"] == "failed"
    assert new_terminal is not None and new_terminal.status == "completed"
    assert new_payload is not None and new_payload["status"] == "completed"


def test_failed_terminal_resumes_after_current_pointer_gained_later_lease(
    tmp_path: Path,
) -> None:
    """A later lease pointer must not hide the failed immutable terminal."""

    run_id = "same-run-later-lease"
    session_id = "public-reporting:module-2.4"
    workflow_id = "public-reporting"
    identity_key = "module-2.4-specialist"
    envelope = TaskEnvelope(
        task_id="module-revision-r1-2.4",
        task_attempt_id="attempt-failed",
        run_id=run_id,
        agent_id=identity_key,
        objective="Revise module 2.4.",
    )
    store = TaskAttemptStore(tmp_path, run_id)

    failed = ProviderTaskAttempt.acquire(
        tmp_path,
        envelope,
        workflow_id=workflow_id,
        identity_key=identity_key,
        session_id=session_id,
    )
    try:
        failed.activate()
        store.persist_result(
            failed.correlation,
            _result(
                run_id=run_id,
                session_id=session_id,
                status=AgentRunStatus.FAILED,
            ).model_dump(mode="json"),
            status="failed",
        )
    finally:
        failed.close()

    manager = IdentityLeaseManager(tmp_path, run_id)
    later_lease = manager.acquire(
        workflow_id,
        identity_key,
        owner_id="later-process:attempt-failed",
    )
    try:
        later_pointer = build_task_correlation(
            tmp_path,
            envelope,
            workflow_id=workflow_id,
            identity_key=identity_key,
            session_id=session_id,
            identity_lease=later_lease.lease,
        )
        store.activate(later_pointer)
    finally:
        later_lease.release()

    assert later_pointer.task_attempt_id == failed.correlation.task_attempt_id
    assert later_pointer.lease_epoch > failed.correlation.lease_epoch
    with pytest.raises(
        RuntimeError,
        match="persisted task attempt identity or hash mismatch",
    ):
        store.load_verified_result(later_pointer)

    resumed = ProviderTaskAttempt.acquire(
        tmp_path,
        envelope,
        workflow_id=workflow_id,
        identity_key=identity_key,
        session_id=session_id,
    )
    try:
        assert resumed.correlation.task_attempt_id != failed.correlation.task_attempt_id
        assert resumed.correlation.run_id == failed.correlation.run_id
        assert resumed.correlation.task_id == failed.correlation.task_id
        assert resumed.correlation.agent_id == failed.correlation.agent_id
        assert resumed.correlation.session_id == failed.correlation.session_id
        assert resumed.load_completed_or_activate() is None
    finally:
        resumed.close()


def test_checkpoint_attempt_does_not_reactivate_older_failed_result(
    tmp_path: Path,
) -> None:
    """A stale checkpoint attempt must not replace a newer failed current pointer."""

    run_id = "same-run-stale-checkpoint-attempt"
    session_id = "public-reporting:module-2.4"
    workflow_id = "public-reporting"
    identity_key = "module-2.4-specialist"
    old_envelope = TaskEnvelope(
        task_id="module-revision-r1-2.4",
        task_attempt_id="attempt-old-failed",
        run_id=run_id,
        agent_id=identity_key,
        objective="Revise module 2.4.",
    )
    store = TaskAttemptStore(tmp_path, run_id)

    old_failed = ProviderTaskAttempt.acquire(
        tmp_path,
        old_envelope,
        workflow_id=workflow_id,
        identity_key=identity_key,
        session_id=session_id,
    )
    try:
        old_failed.activate()
        store.persist_result(
            old_failed.correlation,
            _result(
                run_id=run_id,
                session_id=session_id,
                status=AgentRunStatus.FAILED,
            ).model_dump(mode="json"),
            status="failed",
        )
    finally:
        old_failed.close()

    newer_envelope = old_envelope.model_copy(
        update={"task_attempt_id": "attempt-newer-failed"}
    )
    newer_failed = ProviderTaskAttempt.acquire(
        tmp_path,
        newer_envelope,
        workflow_id=workflow_id,
        identity_key=identity_key,
        session_id=session_id,
    )
    try:
        newer_failed.activate()
        store.persist_result(
            newer_failed.correlation,
            _result(
                run_id=run_id,
                session_id=session_id,
                status=AgentRunStatus.FAILED,
            ).model_dump(mode="json"),
            status="failed",
        )
    finally:
        newer_failed.close()

    assert store.current(old_envelope.task_id) == newer_failed.correlation

    resumed = ProviderTaskAttempt.acquire(
        tmp_path,
        old_envelope,
        workflow_id=workflow_id,
        identity_key=identity_key,
        session_id=session_id,
    )
    try:
        assert resumed.correlation.task_attempt_id not in {
            old_failed.correlation.task_attempt_id,
            newer_failed.correlation.task_attempt_id,
        }
        assert resumed.load_completed_or_activate() is None
        assert store.current(old_envelope.task_id) == resumed.correlation
    finally:
        resumed.close()
