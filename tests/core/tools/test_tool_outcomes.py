from manyselves.core.tools.outcomes import canonical_terminal_message, normalize_tool_outcome


def test_semantic_failure_is_not_success() -> None:
    outcome = normalize_tool_outcome({"status": "failed", "error": "boom"})
    assert outcome.status == "failed"
    assert outcome.error == "boom"


def test_reporting_and_submission_tools_are_terminal() -> None:
    assert normalize_tool_outcome({"status": "completed"}, "submit_result").terminal
    assert normalize_tool_outcome(
        {"status": "blocked"}, "run_reporting_workflow"
    ).terminal
    outcome = normalize_tool_outcome(
        {"status": "running", "run_id": "report-123"},
        "run_reporting_workflow",
    )
    assert outcome.terminal
    assert "等待工作流终态回传" in canonical_terminal_message(outcome)


def test_submission_correction_is_neither_success_nor_terminal() -> None:
    outcome = normalize_tool_outcome(
        {"status": "correction_required", "accepted": False},
        "submit_result",
    )

    assert outcome.status == "correction"
    assert not outcome.terminal
    assert outcome.error is None


def test_non_reporting_running_tool_remains_nonterminal() -> None:
    assert not normalize_tool_outcome({"status": "running"}, "ordinary_tool").terminal


def test_reporting_status_snapshot_ends_current_turn() -> None:
    outcome = normalize_tool_outcome(
        {"status": "in_progress", "run_id": "report-123"},
        "get_reporting_workflow_status",
    )
    assert outcome.terminal
    assert "继续等待工作流终态回传" in canonical_terminal_message(outcome)
