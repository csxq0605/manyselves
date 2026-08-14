"""Shared three-stage chapter cohort: lanes, exact barrier, deterministic merge."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from pydantic import BaseModel


T = TypeVar("T", bound=BaseModel)
M = TypeVar("M", bound=BaseModel)

CHAPTER_SECTION_IDS: dict[str, tuple[str, ...]] = {
    "1": ("1.1", "1.2", "1.3"),
    "3": ("3.1.1", "3.1.2", "3.1.3", "3.2"),
    "4": ("4",),
}


class ChapterCohortError(RuntimeError):
    """One or more chapter lanes failed after all dispatched siblings drained."""


def chapter_for_section(section_id: str) -> str:
    chapter = section_id.split(".", 1)[0]
    if chapter not in CHAPTER_SECTION_IDS:
        raise ValueError(f"unsupported chief-owned section: {section_id}")
    return chapter


def active_chapters(*, include_chapter_four: bool) -> tuple[str, ...]:
    return ("1", "3", "4") if include_chapter_four else ("1", "3")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(value) + b"\n")


async def run_chapter_cohort(
    *,
    workspace: Path,
    run_id: str,
    stage_id: str,
    chapters: tuple[str, ...],
    result_type: type[T],
    run_lane: Callable[[str], Awaitable[T]],
    merge: Callable[[dict[str, T]], M],
) -> tuple[M, str]:
    """Run the same exact three stages for Chief and Final chapter cohorts.

    1. Run/recover independent chapter lanes and drain every dispatched sibling.
    2. Commit one exact barrier only when every active chapter has a valid result.
    3. Merge in canonical chapter order and persist the single promoted result.
    """

    if not chapters or len(chapters) != len(set(chapters)):
        raise ValueError("chapter cohort requires a non-empty unique chapter list")
    if any(chapter not in CHAPTER_SECTION_IDS for chapter in chapters):
        raise ValueError(f"chapter cohort contains unsupported chapters: {chapters}")
    root = Path(workspace).resolve() / "Work" / "runs" / run_id / "lanes" / stage_id

    async def one(chapter: str) -> T:
        result_path = root / f"chapter-{chapter}" / "result.json"
        if result_path.is_file():
            try:
                return result_type.model_validate_json(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        result = await run_lane(chapter)
        if not isinstance(result, result_type):
            raise TypeError(
                f"{stage_id} Chapter {chapter} returned {type(result).__name__}; "
                f"expected {result_type.__name__}"
            )
        _write_json(result_path, result.model_dump(mode="json"))
        return result

    drained = await asyncio.gather(
        *(one(chapter) for chapter in chapters),
        return_exceptions=True,
    )
    failures = {
        chapter: str(value)
        for chapter, value in zip(chapters, drained, strict=True)
        if isinstance(value, BaseException)
    }
    if failures:
        _write_json(
            root / "failed-barrier.json",
            {
                "kind": "chapter_cohort_failed_barrier",
                "run_id": run_id,
                "stage_id": stage_id,
                "chapters": list(chapters),
                "failures": failures,
            },
        )
        raise ChapterCohortError(
            f"{stage_id} chapter lanes failed after sibling drain: {failures}"
        )

    results = {
        chapter: value
        for chapter, value in zip(chapters, drained, strict=True)
        if isinstance(value, result_type)
    }
    if set(results) != set(chapters):
        raise ChapterCohortError(f"{stage_id} did not return every exact chapter result")
    result_refs = {
        chapter: f"Work/runs/{run_id}/lanes/{stage_id}/chapter-{chapter}/result.json"
        for chapter in chapters
    }
    result_hashes = {
        chapter: hashlib.sha256((Path(workspace) / ref).read_bytes()).hexdigest()
        for chapter, ref in result_refs.items()
    }
    barrier_body = {
        "kind": "chapter_cohort_barrier",
        "run_id": run_id,
        "stage_id": stage_id,
        "chapters": list(chapters),
        "result_refs": result_refs,
        "result_hashes": result_hashes,
    }
    barrier = {
        **barrier_body,
        "barrier_sha256": hashlib.sha256(_canonical_bytes(barrier_body)).hexdigest(),
    }
    barrier_path = root / "barrier.json"
    _write_json(barrier_path, barrier)

    merged = merge({chapter: results[chapter] for chapter in chapters})
    _write_json(root / "merged.json", merged.model_dump(mode="json"))
    return merged, barrier_path.relative_to(Path(workspace).resolve()).as_posix()
