from manyselves.core.tools.outcomes import normalize_tool_outcome


def test_semantic_failure_is_not_success() -> None:
    outcome = normalize_tool_outcome({"status": "failed", "error": "boom"})
    assert outcome.status == "failed"
    assert outcome.error == "boom"


def test_reporting_and_submission_tools_are_terminal() -> None:
    assert normalize_tool_outcome({"status": "completed"}, "submit_result").terminal
    assert normalize_tool_outcome({"status": "blocked"}, "run_reporting_workflow").terminal
