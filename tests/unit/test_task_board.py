import pytest

from pds_report.workflow.tasks import InvalidTaskTransitionError, TaskBoard, TaskStatus


def test_task_cannot_start_before_dependencies_complete() -> None:
    board = TaskBoard()
    board.create("a", "agent-a")
    board.create("b", "agent-b", needs=["a"])

    with pytest.raises(InvalidTaskTransitionError, match="dependencies"):
        board.start("b")


def test_task_moves_through_valid_lifecycle() -> None:
    board = TaskBoard()
    board.create("a", "agent-a")

    board.start("a")
    board.complete("a", result_keys=["project_manifest"])

    task = board.get("a")
    assert task.status is TaskStatus.COMPLETED
    assert task.result_keys == ["project_manifest"]


def test_completed_task_rejects_second_completion() -> None:
    board = TaskBoard()
    board.create("a", "agent-a")
    board.start("a")
    board.complete("a")

    with pytest.raises(InvalidTaskTransitionError, match="completed"):
        board.complete("a")


def test_failed_task_skips_transitive_dependents() -> None:
    board = TaskBoard()
    board.create("a", "agent-a")
    board.create("b", "agent-b", needs=["a"])
    board.create("c", "agent-c", needs=["b"])
    board.start("a")

    board.fail("a", "boom")
    skipped = board.skip_dependents("a")

    assert skipped == ["b", "c"]
    assert board.get("b").status is TaskStatus.SKIPPED
    assert board.get("c").status is TaskStatus.SKIPPED


def test_blocked_task_keeps_reason_and_revision() -> None:
    board = TaskBoard()
    board.create("a", "agent-a", revision=2)

    board.block("a", "缺少保护定值表")

    task = board.get("a")
    assert task.status is TaskStatus.BLOCKED
    assert task.blocked_reason == "缺少保护定值表"
    assert task.revision == 2


def test_snapshot_is_json_serializable_shape() -> None:
    board = TaskBoard()
    board.create("a", "agent-a")

    assert board.snapshot() == [
        {
            "id": "a",
            "agent_id": "agent-a",
            "needs": [],
            "status": "pending",
            "error": None,
            "blocked_reason": None,
            "revision": 0,
            "result_keys": [],
        }
    ]
