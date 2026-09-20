import json
import multiprocessing
from pathlib import Path

from manyselves.runtime.usage_ledger import RoundReason, UsageLedger


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


def test_usage_ledger_round_reason_and_attempt_kind_are_summarizable(tmp_path) -> None:
    ledger = UsageLedger(tmp_path, "run-round-reasons")
    ledger.record_attempt(
        status="success",
        attempt=1,
        round_reason=RoundReason.direct_submit,
        reason_source="workflow",
        logical_round_id="round-1",
        parent_provider_call_id=None,
        attempt_kind="provider_request",
        provider_request_sent=True,
        context_manifest_version=4,
        new_chars=12,
        repeated_chars=8,
        repeated_stable_chars=3,
        repeated_dynamic_chars=5,
        duplicate_tool_result_chars=2,
        duplicate_completed_result_chars=4,
        duplicate_evidence_chars=6,
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
    )
    ledger.record_attempt(
        status="error",
        attempt=1,
        attempt_kind="pre_send_block",
        provider_request_sent=False,
        pre_send_guard_status="blocked",
        input_tokens=999,
        output_tokens=999,
        total_tokens=1998,
        new_chars=7,
    )

    by_reason = ledger.summarize(group_by="round_reason")
    assert by_reason["groups"]["direct_submit"]["provider_attempts"] == 1
    assert by_reason["groups"]["direct_submit"]["new_chars"] == 12
    assert by_reason["groups"]["unclassified"]["provider_attempts"] == 0
    assert by_reason["groups"]["unclassified"]["input_tokens"] == 0
    assert by_reason["groups"]["unclassified"]["new_chars"] == 7
    assert by_reason["totals"]["provider_attempts"] == 1
    assert by_reason["totals"]["input_tokens"] == 100
    assert by_reason["totals"]["duplicate_tool_result_chars"] == 2

    by_kind = ledger.summarize(group_by="attempt_kind")
    assert by_kind["groups"]["provider_request"]["provider_attempts"] == 1
    assert by_kind["groups"]["pre_send_block"]["provider_attempts"] == 0


def test_usage_ledger_rejects_provider_retry_before_second_attempt(tmp_path) -> None:
    ledger = UsageLedger(tmp_path, "run-invalid-retry")

    try:
        ledger.record_attempt(
            status="error",
            attempt=1,
            round_reason=RoundReason.provider_retry,
            provider_request_sent=True,
        )
    except ValueError as exc:
        assert "attempt >= 2" in str(exc)
    else:
        raise AssertionError("provider_retry must require attempt >= 2")


def test_usage_ledger_does_not_infer_retry_from_accepted_or_unknown(tmp_path) -> None:
    ledger = UsageLedger(tmp_path, "run-ambiguous")
    row = ledger.record_attempt(
        status="error",
        attempt=2,
        attempt_disposition="accepted_or_unknown",
        retry=True,
        provider_request_sent=True,
    )

    assert row["round_reason"] is None
    assert row["reason_source"] == "unclassified"
    summary = ledger.summarize(group_by="round_reason")
    assert "provider_retry" not in summary["groups"]
    assert summary["groups"]["unclassified"]["provider_attempts"] == 1


def test_usage_ledger_marks_legacy_jsonl_without_guessing_round_reason(tmp_path) -> None:
    ledger = UsageLedger(tmp_path, "run-legacy-round")
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        json.dumps(
            {
                "stage": "legacy-stage",
                "status": "success",
                "input_tokens": 75,
                "output_tokens": 5,
                "total_tokens": 80,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    row = ledger.rows()[0]
    assert row["reason_source"] == "legacy_unclassified"
    assert "round_reason" not in row
    summary = ledger.summarize(group_by="round_reason")
    assert summary["groups"]["legacy_unclassified"]["provider_attempts"] == 1
    assert summary["groups"]["legacy_unclassified"]["input_tokens"] == 75
