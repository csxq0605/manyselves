from manyselves.core.loops.bus import MessageBus
from manyselves.core.reporting.message_router import WorkflowMessageRouter


def test_router_close_removes_every_subscription() -> None:
    bus = MessageBus()
    router = WorkflowMessageRouter(bus, "wf")
    callbacks_before = sum(len(items) for items in bus._subscribers.values())
    assert callbacks_before == 5
    router.close()
    assert sum(len(items) for items in bus._subscribers.values()) == 0


def test_same_identity_sessions_remain_distinct() -> None:
    router = WorkflowMessageRouter(MessageBus(), "wf")
    router.register_session("auditor", "s1", "runtime-1")
    router.register_session("auditor", "s2", "runtime-2")
    assert router._sessions[("auditor", "s1")] == "runtime-1"
    assert router._sessions[("auditor", "s2")] == "runtime-2"
    router.close()
