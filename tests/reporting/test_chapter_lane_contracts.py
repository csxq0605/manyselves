from __future__ import annotations

import pytest
from pydantic import ValidationError

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ChiefChapterLaneRevisionSubmission,
    ChiefChapterLaneSubmission,
    FinalChapterLaneFindingSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ChiefChapterLaneInput,
    FinalChapterLaneInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    SpecialTopicPlan,
    SpecialTopicSectionRequirement,
)

RUN = "run-lane-contract"
SUBJECT = f"Work/runs/{RUN}/edited-revisions/chief-r0.json"


def _plan() -> SpecialTopicPlan:
    return SpecialTopicPlan(
        source_ref="Inputs/special-topic.md",
        source_sha256="0" * 64,
        sections=[
            SpecialTopicSectionRequirement(
                section_id="4.1",
                title="专项主题",
                requirement="说明专项主题的证据边界、影响机制和可执行建议。",
            )
        ],
    )


def test_chief_static_lane_uses_runtime_part_ids_only() -> None:
    submission = ChiefChapterLaneSubmission(
        run_id=RUN,
        chapter_id="1",
        section_ids=["1.1", "1.2", "1.3"],
        part_refs={
            "assessment_background": "Work/runs/run-lane-contract/results/1.1.md",
            "findings_overview": "Work/runs/run-lane-contract/results/1.2.md",
            "regional_executive_summary": "Work/runs/run-lane-contract/results/1.3.md",
        },
    )
    assert set(submission.part_refs) == {
        "assessment_background",
        "findings_overview",
        "regional_executive_summary",
    }
    with pytest.raises(ValidationError):
        ChiefChapterLaneSubmission(
            run_id=RUN,
            chapter_id="1",
            section_ids=["1.1", "1.2", "1.3"],
            part_refs={"1.1": "Work/runs/run-lane-contract/results/1.1.md"},
        )


def test_chapter_four_is_dynamic_and_uses_one_aggregate_part() -> None:
    plan = _plan()
    initial = ChiefChapterLaneInput(
        run_id=RUN,
        subject_ref=SUBJECT,
        chapter_id="4",
        section_ids=["4.1"],
        source_context={"special_topic": "专项主题输入的局部证据摘要。"},
        special_topic_plan=plan,
    )
    assert initial.section_bodies == {}
    output = ChiefChapterLaneSubmission(
        run_id=RUN,
        chapter_id="4",
        section_ids=["4.1"],
        part_refs={"special_topic_analysis": "Work/runs/run-lane-contract/results/ch4.md"},
    )
    assert output.part_refs == {"special_topic_analysis": "Work/runs/run-lane-contract/results/ch4.md"}
    with pytest.raises(ValidationError, match="requires special_topic_plan"):
        ChiefChapterLaneInput(
            run_id=RUN,
            subject_ref=SUBJECT,
            chapter_id="4",
            section_ids=["4.1"],
            source_context={"special_topic": "局部证据摘要。"},
        )


def test_final_lane_cannot_receive_another_chapter() -> None:
    lane = FinalChapterLaneInput(
        run_id=RUN,
        subject_ref=SUBJECT,
        chapter_id="3",
        review_focus=["检查第三章建议是否与证据和优先级一致。"],
        section_ids=["3.1.1", "3.1.2", "3.1.3", "3.2"],
        section_bodies={section_id: "当前章节正文。" for section_id in ("3.1.1", "3.1.2", "3.1.3", "3.2")},
    )
    assert set(lane.section_bodies) == set(lane.section_ids)
    with pytest.raises(ValidationError):
        FinalChapterLaneInput(
            run_id=RUN,
            subject_ref=SUBJECT,
            chapter_id="3",
            review_focus=["检查第三章建议是否与证据和优先级一致。"],
            section_ids=["3.1.1", "4.1"],
            section_bodies={"3.1.1": "正文。", "4.1": "不应进入本 lane。"},
        )


def test_final_chapter_four_finding_is_lane_local() -> None:
    plan = _plan()
    finding = {
        "id": "F-4-001",
        "target_section_ids": ["4.1"],
        "target_changes": [
            {
                "target_section_id": "4.1",
                "required_change": "补充专项主题的证据边界、影响机制和可验证建议。",
                "reviewer_checks": ["专项主题正文与输入要求逐项对应"],
            }
        ],
        "category": "traceability",
        "impact": "blocking",
        "observation": "专项主题正文没有说明证据边界，读者无法判断结论适用范围。",
        "evidence_refs": ["Work/runs/run-lane-contract/reviews/final-4.json"],
    }
    result = FinalChapterLaneFindingSubmission(
        run_id=RUN,
        chapter_id="4",
        checked_section_ids=["4.1"],
        findings=[finding],
    )
    assert result.findings[0].target_section_ids == ["4.1"]
    # The output itself stays lane-local; the active-plan check belongs to its input.
    assert plan.sections[0].section_id == "4.1"
