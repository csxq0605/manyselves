import json
import multiprocessing
from pathlib import Path

from manyselves.core.usage_ledger import UsageLedger


def _append_usage_rows(workspace: str, worker: int, count: int) -> None:
    ledger = UsageLedger(Path(workspace), "run-process-safe")
    for index in range(count):
        ledger.record_attempt(
            run_id="run-process-safe",
            task_id=f"worker-{worker}-{index}",
            stage="process",
            status="success",
        )


def test_usage_ledger_summarizes_stage_and_repeated_request_cost(tmp_path) -> None:
    ledger = UsageLedger(tmp_path, "run-cost")
    common = {
        "run_id": "run-cost",
        "task_id": "module-2.1-review-r0",
        "agent_id": "evidence-auditor--session-1",
        "stage": "module_review",
        "phase": "tool_followup",
        "model": "test-model",
        "request_fingerprint": "request-a",
        "message_fingerprint": "messages-a",
        "tool_schema_fingerprint": "tools-a",
        "message_chars": 1000,
        "tool_schema_chars": 200,
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "cache_write_input_tokens": 0,
        "output_tokens": 10,
        "total_tokens": 110,
        "duration_ms": 50,
        "response_tool_call_count": 1,
    }
    ledger.record_attempt(status="success", **common)
    ledger.record_attempt(status="success", **common)
    ledger.record_attempt(
        **{
            **common,
            "task_id": "module-2.1-revision-r1",
            "stage": "module_revision",
            "status": "error",
            "request_fingerprint": "request-b",
            "message_fingerprint": "messages-b",
        }
    )

    summary = ledger.summarize(group_by="stage")

    assert summary["totals"]["provider_attempts"] == 3
    assert summary["totals"]["successful_attempts"] == 2
    assert summary["totals"]["failed_attempts"] == 1
    assert summary["totals"]["total_tokens"] == 330
    assert summary["totals"]["uncached_input_tokens"] == 240
    assert summary["totals"]["duplicate_request_attempts"] == 1
    assert summary["totals"]["repeated_request_chars"] == 1200
    assert summary["totals"]["repeated_message_chars"] == 1000
    assert summary["totals"]["repeated_tool_schema_chars"] == 400
    assert summary["totals"]["pricing_status"] == "unconfigured"
    assert summary["totals"]["pricing_table_version"] is None
    assert summary["totals"]["pricing_currency"] is None
    assert summary["totals"]["estimated_cost"] is None
    assert summary["groups"]["module_review"]["provider_attempts"] == 2
    assert (
        summary["groups"]["module_review"]["repeated_request_chars"]
        == 1200
    )
    assert (
        summary["groups"]["module_review"]["repeated_message_chars"]
        == 1000
    )
    assert (
        summary["groups"]["module_review"]["repeated_tool_schema_chars"]
        == 200
    )
    assert summary["groups"]["module_revision"]["provider_attempts"] == 1
    assert summary["groups"]["module_review"]["uncached_input_tokens"] == 160
    assert summary["groups"]["module_revision"]["uncached_input_tokens"] == 80
    assert (
        summary["groups"]["module_revision"][
            "repeated_tool_schema_chars"
        ]
        == 200
    )


def test_usage_ledger_derives_uncached_tokens_for_legacy_rows(tmp_path) -> None:
    ledger = UsageLedger(tmp_path, "run-legacy-cost")
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        json.dumps(
            {
                "stage": "legacy-stage",
                "status": "success",
                "input_tokens": 75,
                "cached_input_tokens": 20,
                "cache_write_input_tokens": 5,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert "uncached_input_tokens" not in ledger.rows()[0]
    summary = ledger.summarize()
    assert summary["totals"]["uncached_input_tokens"] == 50
    assert summary["groups"]["legacy-stage"]["uncached_input_tokens"] == 50


def test_usage_ledger_rejects_unknown_grouping(tmp_path) -> None:
    ledger = UsageLedger(tmp_path, "run-cost")

    try:
        ledger.summarize(group_by="unknown")
    except ValueError as exc:
        assert "group_by" in str(exc)
    else:
        raise AssertionError("unknown grouping must fail")


def test_usage_ledger_appends_are_process_safe(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    workers = [
        context.Process(
            target=_append_usage_rows,
            args=(str(tmp_path), worker, 25),
        )
        for worker in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0

    rows = UsageLedger(tmp_path, "run-process-safe").rows()
    assert len(rows) == 100
    assert len({row["task_id"] for row in rows}) == 100
