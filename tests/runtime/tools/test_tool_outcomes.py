from manyselves.runtime.tools.outcomes import normalize_tool_outcome


def test_semantic_failure_is_not_success() -> None:
    outcome = normalize_tool_outcome({"status": "failed", "error": "boom"})
    assert outcome.status == "failed"
    assert outcome.error == "boom"


def test_submission_tool_is_terminal() -> None:
    assert normalize_tool_outcome({"status": "completed"}, "submit_result").terminal


def test_submission_correction_is_neither_success_nor_terminal() -> None:
    outcome = normalize_tool_outcome(
        {"status": "correction_required", "accepted": False},
        "submit_result",
    )

    assert outcome.status == "correction"
    assert not outcome.terminal
    assert outcome.error is None


def test_running_tool_remains_nonterminal() -> None:
    assert not normalize_tool_outcome({"status": "running"}, "ordinary_tool").terminal
