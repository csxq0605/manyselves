import asyncio
import json

import pytest
from pydantic import BaseModel

from manyselves.core.reporting.chapter_parallel import (
    ChapterCohortError,
    run_chapter_cohort,
)


class LaneResult(BaseModel):
    chapter: str
    value: str


class MergedResult(BaseModel):
    chapters: list[str]
    values: list[str]


@pytest.mark.asyncio
async def test_chief_and_final_share_exact_lane_barrier_merge_protocol(tmp_path):
    calls: list[str] = []

    async def lane(chapter: str) -> LaneResult:
        calls.append(chapter)
        await asyncio.sleep({"1": 0.02, "3": 0.01, "4": 0}[chapter])
        return LaneResult(chapter=chapter, value=f"body-{chapter}")

    def merge(results: dict[str, LaneResult]) -> MergedResult:
        return MergedResult(
            chapters=list(results),
            values=[result.value for result in results.values()],
        )

    for stage_id in ("chief-edit-r0", "final-review-initial-r0"):
        merged, barrier_ref = await run_chapter_cohort(
            workspace=tmp_path,
            run_id="report-test",
            stage_id=stage_id,
            chapters=("1", "3", "4"),
            result_type=LaneResult,
            run_lane=lane,
            merge=merge,
        )
        assert merged.chapters == ["1", "3", "4"]
        assert merged.values == ["body-1", "body-3", "body-4"]
        barrier = json.loads((tmp_path / barrier_ref).read_text(encoding="utf-8"))
        assert barrier["chapters"] == ["1", "3", "4"]
        assert set(barrier["result_hashes"]) == {"1", "3", "4"}
        assert (tmp_path / barrier_ref).with_name("merged.json").is_file()

    assert sorted(calls) == ["1", "1", "3", "3", "4", "4"]


@pytest.mark.asyncio
async def test_chapter_cohort_drains_siblings_and_never_promotes_partial_result(tmp_path):
    completed: list[str] = []

    async def lane(chapter: str) -> LaneResult:
        if chapter == "3":
            raise RuntimeError("lane failed")
        await asyncio.sleep(0.01)
        completed.append(chapter)
        return LaneResult(chapter=chapter, value=chapter)

    with pytest.raises(ChapterCohortError):
        await run_chapter_cohort(
            workspace=tmp_path,
            run_id="report-test",
            stage_id="chief-edit-r0",
            chapters=("1", "3", "4"),
            result_type=LaneResult,
            run_lane=lane,
            merge=lambda results: MergedResult(chapters=list(results), values=[]),
        )

    root = tmp_path / "Work/runs/report-test/lanes/chief-edit-r0"
    assert completed == ["1", "4"]
    assert (root / "failed-barrier.json").is_file()
    assert not (root / "barrier.json").exists()
    assert not (root / "merged.json").exists()

