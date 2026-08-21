from __future__ import annotations

from types import MethodType

from manyselves.core.reporting.workflow import (
    ReportWorkflowRunner,
    _DeliveryContext,
)


def test_deliver_runs_prepare_publish_complete_in_order() -> None:
    runner = object.__new__(ReportWorkflowRunner)
    state = {"run_id": "run-delivery-boundaries"}
    context = _DeliveryContext.model_construct(state=state)
    calls: list[tuple[str, object]] = []

    def prepare(self, received):
        calls.append(("prepare", received))
        return context

    def publish(self, received):
        calls.append(("publish", received))
        return received

    def complete(self, received):
        calls.append(("complete", received))

    runner._prepare_and_render_delivery = MethodType(prepare, runner)
    runner._publish_and_materialize_delivery = MethodType(publish, runner)
    runner._complete_delivery = MethodType(complete, runner)

    runner._deliver(state)

    assert [name for name, _value in calls] == [
        "prepare",
        "publish",
        "complete",
    ]
    assert calls[0][1] is state
    assert calls[1][1] is context
    assert calls[2][1] is context
    assert context.state is state
