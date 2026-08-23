from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TEMPLATE_ROLE_SKILL_IDS,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    FinalChapterLaneInput,
)
from manyselves.core.reporting.final_specialization import (
    FINAL_LANE_SPECIALIZATIONS,
)


def test_chief_template_skills_are_chapter_specific_without_shared_compat_id() -> None:
    assert len(TEMPLATE_ROLE_SKILL_IDS) == 14
    assert "chief-editor" not in TEMPLATE_ROLE_SKILL_IDS
    assert {
        "chief-editor-chapter-1",
        "chief-editor-chapter-3",
        "chief-editor-chapter-4",
    }.issubset(TEMPLATE_ROLE_SKILL_IDS)


def test_final_lanes_share_one_skill_but_have_distinct_review_focus() -> None:
    assert set(FINAL_LANE_SPECIALIZATIONS) == {"1", "3", "4"}
    focuses = {
        specialization.review_focus
        for specialization in FINAL_LANE_SPECIALIZATIONS.values()
    }
    assert len(focuses) == 3
    for chapter_id, specialization in FINAL_LANE_SPECIALIZATIONS.items():
        context = specialization.prompt_context()
        assert f'chapter_id="{chapter_id}"' in context
        assert "完整 final-auditor Skill" in context
        assert "不扩大 required_section_ids" in context


def test_parallel_final_contract_requires_chapter_review_focus() -> None:
    payload = {
        "kind": "final_chapter_lane_input",
        "phase": "initial",
        "run_id": "report-example",
        "subject_ref": "Work/runs/report-example/edited.json",
        "chapter_id": "1",
        "review_focus": [],
        "section_ids": ["1.1", "1.2", "1.3"],
        "section_bodies": {"1.1": "a", "1.2": "b", "1.3": "c"},
        "required_findings": [],
        "revision_responses": [],
        "special_topic_plan": None,
        "revision": 0,
    }
    with pytest.raises(ValidationError, match="chapter review focus"):
        FinalChapterLaneInput.model_validate(payload)
