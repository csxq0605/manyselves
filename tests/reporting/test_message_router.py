from manyselves.capabilities.distribution_reporting.runtime.message_router import (
    WorkflowMessageRouter,
)
from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import ProgressNoteMessage


def test_router_close_removes_every_subscription() -> None:
    bus = MessageBus()
    router = WorkflowMessageRouter(bus, "wf")
    callbacks_before = sum(len(items) for items in bus._subscribers.values())
    assert callbacks_before == 4
    router.close()
    assert sum(len(items) for items in bus._subscribers.values()) == 0


def test_same_identity_sessions_remain_distinct() -> None:
    router = WorkflowMessageRouter(MessageBus(), "wf")
    router.register_session("auditor", "s1", "runtime-1")
    router.register_session("auditor", "s2", "runtime-2")
    assert router._sessions[("auditor", "s1")] == "runtime-1"
    assert router._sessions[("auditor", "s2")] == "runtime-2"
    router.close()


def test_router_does_not_treat_ledger_source_ids_as_artifact_paths() -> None:
    router = WorkflowMessageRouter(MessageBus(), "wf")

    router._consume_progress(
        ProgressNoteMessage(
            workflow_id="wf",
            task_id="module-2.3",
            sender="module-2.3-specialist",
            note_kind="gap",
            artifact_refs=[
                "Work/runs/run-1/gaps/GAP-1.json",
                "E-0008",
                "R-001",
                "W-web-source",
            ],
        )
    )

    assert router.gaps == ["Work/runs/run-1/gaps/GAP-1.json"]
    router.close()
