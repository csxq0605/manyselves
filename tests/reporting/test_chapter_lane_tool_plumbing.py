from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    CHAPTER3_SECTION_IDS,
    SpecialTopicPlan,
)
from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider, LLMResponse
from manyselves.core.reporting.agent_runner import ReportingAgentRunner
from manyselves.core.reporting.config import load_packaged_agents
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.tools.reporting_collaboration_tools import (
    ListResultPartsTool,
    SubmitResultTool,
    WriteResultPartTool,
)

RUN = "run-chapter-tools"
CHIEF_SUBJECT = f"Work/runs/{RUN}/edited-revisions/chief-r0.json"


class _Provider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("test", model="chapter-lane-test")

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        return LLMResponse(content="")


def _write_contract(workspace: Path, contract) -> str:
    ref = f"Work/runs/{RUN}/context/{contract.kind}.json"
    ReportingStore(workspace).write_json(ref, contract.model_dump(mode="json"))
    return ref


def _chief_initial() -> ChiefChapterLaneInput:
    return ChiefChapterLaneInput(
        phase="initial",
        run_id=RUN,
        subject_ref=CHIEF_SUBJECT,
        chapter_id="3",
        section_ids=list(CHAPTER3_SECTION_IDS),
        source_context={"cross_summary": "仅供 Chapter 3 lane 使用的边界摘要。"},
    )


def _chapter_four_plan() -> SpecialTopicPlan:
    return SpecialTopicPlan(
        source_ref="Inputs/topic.md",
        source_sha256="0" * 64,
        sections=[
            {
                "section_id": "4.1",
                "title": "Dynamic topic",
                "requirement": "Explain the project boundary and verification method.",
            }
        ],
    )


def _chief_chapter_four() -> ChiefChapterLaneInput:
    plan = _chapter_four_plan()
    return ChiefChapterLaneInput(
        phase="initial",
        run_id=RUN,
        subject_ref=CHIEF_SUBJECT,
        chapter_id="4",
        section_ids=["4.1"],
        source_context={"special_topic": "Only the active Chapter 4 evidence summary."},
        special_topic_plan=plan,
    )


async def _write_chief_parts(
    workspace: Path,
    *,
    task_id: str,
    revision: int,
    part_ids: list[str],
) -> dict[str, str]:
    store = ReportingStore(workspace)
    writer = WriteResultPartTool(
        RUN,
        task_id,
        revision,
        store,
        part_ids,
    )
    refs: dict[str, str] = {}
    for part_id in part_ids:
        result = await writer(part_id, f"### {part_id}\n\n完整章节正文。")
        refs[part_id] = result["artifact_ref"]
    return refs


def _submit_tool(
    workspace: Path,
    *,
    task_id: str,
    revision: int,
    kind: str,
    contract_kind: str,
    contract_ref: str,
) -> SubmitResultTool:
    return SubmitResultTool(
        "chief-editor",
        "session-chapter",
        RUN,
        task_id,
        ReportingStore(workspace),
        MessageBus(),
        "workflow-chapter",
        allowed_outputs=[kind],
        revision=revision,
        input_contract_kind=contract_kind,
        input_contract_ref=contract_ref,
    )


@pytest.mark.asyncio
async def test_chief_initial_and_revision_are_assembled_from_saved_parts(tmp_path: Path) -> None:
    initial_contract = _chief_initial()
    initial_ref = _write_contract(tmp_path, initial_contract)
    part_ids = ["risk_panorama", "dimension_risk_analysis", "data_gap_analysis", "improvement_action_plan"]
    initial_task = "chief-ch3-initial"
    initial_refs = await _write_chief_parts(
        tmp_path,
        task_id=initial_task,
        revision=0,
        part_ids=part_ids,
    )
    tool = _submit_tool(
        tmp_path,
        task_id=initial_task,
        revision=0,
        kind="chief_chapter_lane_submission",
        contract_kind=initial_contract.kind,
        contract_ref=initial_ref,
    )
    accepted = await tool(
        **{
            "kind": "chief_chapter_lane_submission",
            "run_id": RUN,
            "chapter_id": "3",
            "section_ids": list(CHAPTER3_SECTION_IDS),
            "part_refs": initial_refs,
            "revision": 0,
        }
    )
    assert accepted["status"] == "completed"
    result = json.loads(
        (tmp_path / f"Work/runs/{RUN}/results/{initial_task}.json").read_text(
            encoding="utf-8"
        )
    )
    assert result["payload"]["part_refs"] == initial_refs

    finding = {
        "id": "F-3-001",
        "target_section_ids": ["3.2"],
        "target_changes": [
            {
                "target_section_id": "3.2",
                "required_change": "补充可验证的整改顺序、责任边界与验收条件。",
                "reviewer_checks": ["整改顺序、责任边界和验收条件均已明确"],
            }
        ],
        "category": "traceability",
        "impact": "blocking",
        "observation": "当前行动计划缺少可核验的责任边界与验收条件，无法直接执行。",
        "evidence_refs": [CHIEF_SUBJECT],
    }
    revision_contract = ChiefChapterLaneInput(
        phase="revision",
        run_id=RUN,
        subject_ref=CHIEF_SUBJECT,
        chapter_id="3",
        section_ids=list(CHAPTER3_SECTION_IDS),
        section_bodies={section_id: "当前章节正文。" for section_id in CHAPTER3_SECTION_IDS},
        assigned_findings=[finding],
        revision=1,
    )
    revision_ref = _write_contract(tmp_path, revision_contract)
    revision_task = "chief-ch3-revision"
    revision_refs = await _write_chief_parts(
        tmp_path,
        task_id=revision_task,
        revision=1,
        part_ids=["improvement_action_plan"],
    )
    revision_tool = _submit_tool(
        tmp_path,
        task_id=revision_task,
        revision=1,
        kind="chief_chapter_lane_revision_submission",
        contract_kind=revision_contract.kind,
        contract_ref=revision_ref,
    )
    revised = await revision_tool(
        **{
            "kind": "chief_chapter_lane_revision_submission",
            "run_id": RUN,
            "base_subject_ref": CHIEF_SUBJECT,
            "chapter_id": "3",
            "revision": 1,
            "section_ids": ["3.2"],
            "part_refs": revision_refs,
            "revision_responses": [
                {
                    "finding_id": "F-3-001",
                    "action": "implemented",
                    "summary": "已补充整改顺序、责任边界和可验证的验收条件。",
                    "changed_target_ids": ["3.2"],
                }
            ],
        }
    )
    assert revised["status"] == "completed"
    revision_result = json.loads(
        (tmp_path / f"Work/runs/{RUN}/results/{revision_task}.json").read_text(
            encoding="utf-8"
        )
    )
    assert revision_result["payload"]["part_refs"] == revision_refs


@pytest.mark.asyncio
async def test_final_chapter_lane_initial_and_recheck_enforce_scope(tmp_path: Path) -> None:
    initial_contract = FinalChapterLaneInput(
        phase="initial",
        run_id=RUN,
        subject_ref=CHIEF_SUBJECT,
        chapter_id="3",
        review_focus=["检查第三章建议是否与证据和优先级一致。"],
        section_ids=list(CHAPTER3_SECTION_IDS),
        section_bodies={section_id: "章节正文。" for section_id in CHAPTER3_SECTION_IDS},
    )
    initial_ref = _write_contract(tmp_path, initial_contract)
    tool = _submit_tool(
        tmp_path,
        task_id="final-ch3-initial",
        revision=0,
        kind="final_chapter_lane_finding_submission",
        contract_kind=initial_contract.kind,
        contract_ref=initial_ref,
    )
    accepted = await tool(
        **{
            "kind": "final_chapter_lane_finding_submission",
            "run_id": RUN,
            "chapter_id": "3",
            "checked_section_ids": list(CHAPTER3_SECTION_IDS),
            "findings": [],
            "residual_risks": [],
        }
    )
    assert accepted["status"] == "completed"

    stringified_tool = _submit_tool(
        tmp_path,
        task_id="final-ch3-initial-stringified-transport",
        revision=0,
        kind="final_chapter_lane_finding_submission",
        contract_kind=initial_contract.kind,
        contract_ref=initial_ref,
    )
    stringified = await stringified_tool(
        **{
            "kind": "final_chapter_lane_finding_submission",
            "run_id": RUN,
            "chapter_id": "3",
            "checked_section_ids": list(CHAPTER3_SECTION_IDS),
            "findings": json.dumps(
                [
                    {
                        "category": "traceability",
                        "impact": "advisory",
                        "observation": (
                            "当前结论需要补充一条可复核的当前运行证据引用。"
                        ),
                        "evidence_refs": [CHIEF_SUBJECT],
                        "target_section_ids": ["3.2"],
                        "target_changes": [
                            {
                                "target_section_id": "3.2",
                                "required_change": (
                                    "补充当前运行证据引用并说明证据所支持的边界。"
                                ),
                                "reviewer_checks": ["结论能够追溯到当前运行证据。"],
                            }
                        ],
                    }
                ],
                ensure_ascii=False,
            ),
            "residual_risks": [],
        }
    )
    assert stringified["status"] == "completed"
    assert stringified["correction_mode"] == "runtime_transport_normalization"
    assert stringified["transport_normalization"] == {
        "kind": "schema_json_transport_normalization_v1",
        "fields": [{"field": "$.findings", "mode": "exact_json_decode_v1"}],
    }

    bad = await tool(
        **{
            "kind": "final_chapter_lane_finding_submission",
            "run_id": RUN,
            "chapter_id": "3",
            "checked_section_ids": list(CHAPTER3_SECTION_IDS),
            "findings": [
                {
                    "target_section_ids": ["1.1"],
                    "target_changes": [
                        {
                            "target_section_id": "1.1",
                            "required_change": "不应在 Chapter 3 lane 中提交 Chapter 1 修改请求。",
                            "reviewer_checks": ["目标必须属于 Chapter 3"],
                        }
                    ],
                    "category": "scope",
                    "impact": "blocking",
                    "observation": "该 finding 指向另一个章节，超出了当前 Chapter 3 lane 的范围。",
                    "evidence_refs": [CHIEF_SUBJECT],
                }
            ],
            "residual_risks": [],
        }
    )
    assert bad["status"] == "correction_required"
    assert any(
        "chapter lane" in issue["problem"]
        for issue in bad["validation_errors"]
    )

    required = {
        "id": "F-3-001",
        "target_section_ids": ["3.2"],
        "target_changes": [
            {
                "target_section_id": "3.2",
                "required_change": "补充可验证的整改顺序、责任边界与验收条件。",
                "reviewer_checks": ["整改顺序与验收条件已经明确"],
            }
        ],
        "category": "traceability",
        "impact": "blocking",
        "observation": "当前行动计划缺少可核验的责任边界与验收条件，无法直接执行。",
        "evidence_refs": [CHIEF_SUBJECT],
    }
    recheck_contract = FinalChapterLaneInput(
        phase="recheck",
        run_id=RUN,
        subject_ref=CHIEF_SUBJECT,
        chapter_id="3",
        review_focus=["检查第三章修订是否关闭指定 finding。"],
        section_ids=list(CHAPTER3_SECTION_IDS),
        section_bodies={"3.2": "修订后的章节正文。"},
        unchanged_section_sha256={
            section_id: "0" * 64
            for section_id in CHAPTER3_SECTION_IDS
            if section_id != "3.2"
        },
        required_findings=[required],
        revision=1,
    )
    recheck_ref = _write_contract(tmp_path, recheck_contract)
    recheck_tool = _submit_tool(
        tmp_path,
        task_id="final-ch3-recheck",
        revision=1,
        kind="final_chapter_lane_verdict_submission",
        contract_kind=recheck_contract.kind,
        contract_ref=recheck_ref,
    )
    missing = await recheck_tool(
        **{
            "kind": "final_chapter_lane_verdict_submission",
            "run_id": RUN,
            "chapter_id": "3",
            "checked_section_ids": list(CHAPTER3_SECTION_IDS),
            "verdicts": [],
            "new_findings": [],
            "residual_risks": [],
        }
    )
    assert missing["status"] == "correction_required"
    assert "verdicts.finding_id" in missing["validation_errors"][0]["field"]


def test_chapter_lane_schemas_and_expected_result_parts_are_exact() -> None:
    contract = _chief_initial()
    schema = ReportingAgentRunner._task_submission_schema(
        "chief_chapter_lane_submission", contract
    )
    assert schema["properties"]["run_id"]["const"] == RUN
    assert schema["properties"]["chapter_id"]["const"] == "3"
    assert schema["properties"]["section_ids"]["const"] == list(CHAPTER3_SECTION_IDS)
    writer_schema = ReportingAgentRunner._result_part_tool_schema(
        ["improvement_action_plan"],
        evidence_binding_required=False,
        section_body_only=True,
        chapter_id="3",
    )
    description = writer_schema["properties"]["content"]["description"]
    assert "improvement_action_plan -> **3.2.x ...**" in description
    assert "**3.x ...**" in description
    assert "Never use a chapter-level shortcut" in description
    assert "not a rejection rule" in description

    final_contract = FinalChapterLaneInput(
        run_id=RUN,
        subject_ref=CHIEF_SUBJECT,
        chapter_id="3",
        review_focus=["检查第三章建议是否与证据和优先级一致。"],
        section_ids=list(CHAPTER3_SECTION_IDS),
        section_bodies={section_id: "章节正文。" for section_id in CHAPTER3_SECTION_IDS},
    )
    final_schema = ReportingAgentRunner._task_submission_schema(
        "final_chapter_lane_finding_submission", final_contract
    )
    assert final_schema["properties"]["checked_section_ids"]["const"] == list(CHAPTER3_SECTION_IDS)
    assert "id" not in final_schema["$defs"]["ChapterScopedFinalReviewFinding"]["properties"]
    example = final_schema["examples"][0]
    assert isinstance(example["findings"], list)
    assert len(example["findings"]) == 1
    assert "id" not in example["findings"][0]
    assert example["findings"][0]["target_section_ids"] == [CHAPTER3_SECTION_IDS[0]]
    assert example["findings"][0]["target_changes"][0]["target_section_id"] == (
        CHAPTER3_SECTION_IDS[0]
    )
    assert example["findings"][0]["evidence_refs"] == [CHIEF_SUBJECT]


def test_task_envelope_accepts_chapter_lane_input_contract_kinds() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope

    for kind in ("chief_chapter_lane_input", "final_chapter_lane_input"):
        envelope = TaskEnvelope(
            task_id=f"task-{kind}",
            run_id=RUN,
            agent_id="chief-editor",
            objective="chapter lane",
            input_refs=[f"Work/runs/{RUN}/context/{kind}.json"],
            input_contract_kind=kind,
            input_contract_ref=f"Work/runs/{RUN}/context/{kind}.json",
        )
        assert envelope.input_contract_kind == kind


def test_runner_chief_lane_exposes_exact_parts_and_one_output_schema(tmp_path: Path) -> None:
    contract = _chief_initial().model_copy(
        update={
            "chapter_id": "1",
            "section_ids": ["1.1", "1.2", "1.3"],
        }
    )
    # Chapter 1 has static sections and therefore the three corresponding
    # CHIEF_SECTION_RESULT_PART_IDS are the only writable parts.
    contract_ref = _write_contract(tmp_path, contract)
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
    from manyselves.core.reporting.agent_runner import ReportingAgentRunner
    from manyselves.core.reporting.config import load_packaged_agents

    envelope = TaskEnvelope(
        task_id="chief-ch1-schema",
        run_id=RUN,
        agent_id="chief-editor",
        objective="完成 Chapter 1 lane",
        input_refs=[contract_ref],
        allowed_tools=["write_result_part", "list_result_parts", "submit_result"],
        allowed_outputs=["chief_chapter_lane_submission"],
        input_contract_kind=contract.kind,
        input_contract_ref=contract_ref,
    )
    runner = ReportingAgentRunner(
        tmp_path,
        MessageBus(),
        _Provider(),
        AgentDefaults(),
    )
    registry = runner._tools(
        load_packaged_agents()["chief-editor"],
        envelope,
        "session-chapter-schema",
        "workflow-chapter-schema",
    )
    writer_schema = registry._schema_cache["write_result_part"]
    assert writer_schema["properties"]["part_id"]["enum"] == [
        "assessment_background",
        "findings_overview",
        "regional_executive_summary",
    ]
    assert "do not include any numbered Markdown heading" in writer_schema[
        "properties"
    ]["content"]["description"]
    submission_schema = registry._schema_cache["submit_result"]
    assert "oneOf" not in submission_schema
    assert submission_schema["properties"]["chapter_id"]["const"] == "1"
    assert submission_schema["properties"]["section_ids"]["const"] == ["1.1", "1.2", "1.3"]


def test_chief_chapter_four_tool_schema_allows_nested_planned_headings(
    tmp_path: Path,
) -> None:
    contract = _chief_chapter_four()
    contract_ref = _write_contract(tmp_path, contract)
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope

    envelope = TaskEnvelope(
        task_id="chief-ch4-schema",
        run_id=RUN,
        agent_id="chief-editor",
        objective="完成 Chapter 4 lane",
        input_refs=[contract_ref],
        allowed_tools=["write_result_part", "list_result_parts", "submit_result"],
        allowed_outputs=["chief_chapter_lane_submission"],
        input_contract_kind=contract.kind,
        input_contract_ref=contract_ref,
    )
    runner = ReportingAgentRunner(
        tmp_path,
        MessageBus(),
        _Provider(),
        AgentDefaults(),
    )
    registry = runner._tools(
        load_packaged_agents()["chief-editor"],
        envelope,
        "session-chapter-four-schema",
        "workflow-chapter-four-schema",
    )

    description = registry._schema_cache["write_result_part"]["properties"][
        "content"
    ]["description"]
    assert "#### 4.1.1 are allowed" in description
    assert "##### 4.1.1.1" in description
    assert "do not add another top-level 4.n section" in description
    assert "must use these explicit Markdown headings" in description
    assert "never replace it with bold numbered labels" in description


@pytest.mark.asyncio
async def test_chief_numbering_convention_does_not_reject_bold_body_labels(
    tmp_path: Path,
) -> None:
    writer = WriteResultPartTool(
        RUN,
        "chief-numbering-advisory",
        0,
        ReportingStore(tmp_path),
        ["improvement_action_plan"],
        section_body_only=True,
    )

    result = await writer(
        "improvement_action_plan",
        "**1.1 旧式局部编号**\n\n正文仍应被接受并持久化。",
    )

    assert result["status"] == "created"
    assert result["persisted"] is True


@pytest.mark.asyncio
async def test_chief_chapter_four_tools_accept_nested_headings_and_reject_new_top_level(
    tmp_path: Path,
) -> None:
    plan = _chapter_four_plan()
    store = ReportingStore(tmp_path)
    task_id = "chief-ch4-nested"
    writer = WriteResultPartTool(
        RUN,
        task_id,
        0,
        store,
        ["special_topic_analysis"],
        special_topic_plan=plan,
    )
    valid = (
        "## 4. Runtime wrapper\n\nLegacy introduction.\n\n"
        "### 4.1 Dynamic topic\n\n"
        "Complete Chapter 4 analysis with a project boundary and verification method.\n\n"
        "#### 4.1.1 Verification steps\n\n"
        "Record the responsible owner, input, acceptance result, and residual risk."
    )

    accepted = await writer("special_topic_analysis", valid)
    assert accepted["persisted"] is True
    listing = await ListResultPartsTool(
        RUN,
        task_id,
        0,
        store,
        ["special_topic_analysis"],
        special_topic_plan=plan,
    )()
    assert listing["ready_part_ids"] == ["special_topic_analysis"]
    assert listing["rewrite_part_ids"] == []

    rejected = await writer(
        "special_topic_analysis",
        valid
        + "\n\n### 4.2 Unplanned top-level section\n\n"
        + "This top-level section is outside the active Inputs plan.",
    )
    assert rejected["status"] == "correction_required"
    assert rejected["rewrite_part_ids"] == ["special_topic_analysis"]
    assert "exactly match" in rejected["validation_errors"][0]["problem"]


@pytest.mark.asyncio
async def test_chief_chapter_four_submit_accepts_nested_numbered_headings(
    tmp_path: Path,
) -> None:
    contract = _chief_chapter_four()
    contract_ref = _write_contract(tmp_path, contract)
    store = ReportingStore(tmp_path)
    task_id = "chief-ch4-submit-nested"
    ref = f"Work/runs/{RUN}/drafts/{task_id}/r0/special_topic_analysis.md"
    store.write_text(
        ref,
        "### 4.1 Dynamic topic\n\n"
        "Complete Chapter 4 analysis with a project boundary and verification method.\n\n"
        "#### 4.1.1 Verification steps\n\n"
        "Record the responsible owner, input, acceptance result, and residual risk.",
    )
    tool = _submit_tool(
        tmp_path,
        task_id=task_id,
        revision=0,
        kind="chief_chapter_lane_submission",
        contract_kind=contract.kind,
        contract_ref=contract_ref,
    )

    accepted = await tool(
        **{
            "kind": "chief_chapter_lane_submission",
            "run_id": RUN,
            "chapter_id": "4",
            "section_ids": ["4.1"],
            "part_refs": {"special_topic_analysis": ref},
            "revision": 0,
        }
    )
    assert accepted["status"] == "completed"


@pytest.mark.asyncio
async def test_chief_static_lane_rejects_numbered_headings_before_persistence(
    tmp_path: Path,
) -> None:
    store = ReportingStore(tmp_path)
    writer = WriteResultPartTool(
        RUN,
        "chief-static-body",
        0,
        store,
        ["assessment_background"],
        section_body_only=True,
    )

    rejected = await writer(
        "assessment_background",
        "# 1.1 评估背景\n\n正文。\n\n## 1.1.1 范围\n\n补充正文。",
    )

    assert rejected["status"] == "correction_required"
    assert rejected["persisted"] is False
    assert rejected["rewrite_part_ids"] == ["assessment_background"]
    assert "section body only" in rejected["validation_errors"][0]["problem"]
    assert not (
        tmp_path
        / f"Work/runs/{RUN}/drafts/chief-static-body/r0/assessment_background.md"
    ).exists()


@pytest.mark.asyncio
async def test_chief_submit_rejects_preexisting_numbered_static_part(
    tmp_path: Path,
) -> None:
    contract = _chief_initial().model_copy(
        update={"chapter_id": "1", "section_ids": ["1.1", "1.2", "1.3"]}
    )
    contract_ref = _write_contract(tmp_path, contract)
    store = ReportingStore(tmp_path)
    task_id = "chief-preexisting-heading"
    refs = {}
    for part_id in (
        "assessment_background",
        "findings_overview",
        "regional_executive_summary",
    ):
        ref = f"Work/runs/{RUN}/drafts/{task_id}/r0/{part_id}.md"
        store.write_text(
            ref,
            "# 1.1 运行时应拥有此标题\n\n静态 Chief part 正文。"
            if part_id == "assessment_background"
            else "静态 Chief part 正文。",
        )
        refs[part_id] = ref
    tool = _submit_tool(
        tmp_path,
        task_id=task_id,
        revision=0,
        kind="chief_chapter_lane_submission",
        contract_kind=contract.kind,
        contract_ref=contract_ref,
    )

    rejected = await tool(
        **{
            "kind": "chief_chapter_lane_submission",
            "run_id": RUN,
            "chapter_id": "1",
            "section_ids": ["1.1", "1.2", "1.3"],
            "part_refs": refs,
            "revision": 0,
        }
    )

    assert rejected["status"] == "correction_required"
    assert rejected["rewrite_part_ids"] == ["assessment_background"]
    assert "body only" in rejected["validation_errors"][0]["problem"]
