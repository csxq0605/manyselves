from __future__ import annotations

import json
from pathlib import Path

from manyselves.core.reporting.declarative_delivery import (
    DeclarativeDeliveryRuntime,
)
from manyselves.core.reporting.workflow import _DeliveryContext


def _context(state: dict) -> _DeliveryContext:
    path = Path("Work/runs/run-delivery")
    return _DeliveryContext(
        state=state,
        final_audit_snapshot_ref=f"{path}/final-audit.json",
        claim_ledger_path=path / "claims.json",
        source_ledger_path=path / "sources.json",
        evidence_snapshot_path=path / "evidence.jsonl",
        approved_module_paths={},
        edited_submission_path=path / "edited.json",
        request_snapshot_path=path / "request.json",
        photo_manifest_path=path / "photos.json",
        delivery_markdown="# Report",
        report_state_path=path / "report-state.json",
        markdown_path=path / "report.md",
        source_index_markdown="# Sources",
        source_index_path=path / "sources.md",
        source_index_docx_path=path / "sources.docx",
        template_snapshot=path / "template.docx",
        template_provenance_path=path / "template.json",
        output=path / "report.docx",
        render_result_ref=path / "render-result.json",
    )


class _DeliveryRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def _prepare_and_render_delivery(self, state: dict) -> _DeliveryContext:
        self.calls.append("prepare")
        return _context(state)

    def _publish_and_materialize_delivery(
        self,
        context: _DeliveryContext,
    ) -> _DeliveryContext:
        self.calls.append("publish")
        assert context.state["run_id"] == "run-delivery"
        return context

    def _complete_delivery(self, context: _DeliveryContext) -> None:
        self.calls.append("complete")
        context.state["delivery_completion_ref"] = "delivery-completion.json"


def test_delivery_runtime_persists_context_between_declared_actions() -> None:
    runner = _DeliveryRunner()
    state = {"run_id": "run-delivery"}

    prepared = DeclarativeDeliveryRuntime(runner).prepare(state)
    restored = json.loads(json.dumps(prepared))
    published = DeclarativeDeliveryRuntime(runner).publish(restored)
    restored = json.loads(json.dumps(published))
    completed = DeclarativeDeliveryRuntime(runner).complete(restored)

    assert runner.calls == ["prepare", "publish", "complete"]
    assert completed == {
        "run_id": "run-delivery",
        "delivery_completion_ref": "delivery-completion.json",
    }


def test_delivery_runtime_preserves_existing_completion_without_reexecution() -> None:
    runner = _DeliveryRunner()
    state = {
        "run_id": "run-delivery-complete",
        "delivery_completion_ref": "existing-delivery.json",
    }
    runtime = DeclarativeDeliveryRuntime(runner)

    assert runtime.prepare(state) is state
    assert runtime.publish(state) is state
    assert runtime.complete(state) is state
    assert runner.calls == []
