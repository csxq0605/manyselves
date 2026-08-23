from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.photo_bindings import (
    runtime_photo_ids,
)
from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
    compose_module_markdown,
)
from manyselves.capabilities.distribution_reporting.runtime.assets import (
    ReportAssetAssembler,
    validate_editor_protection,
    validate_editor_quality,
    validate_existing_markdown_modules,
    validate_module_markdown_consistency,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ClaimRecord,
    EditedReportSubmission,
    ModuleSubmission,
    TableSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import ValidationReport
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    PhotoAsset,
    SourceLocation,
)
from manyselves.capabilities.distribution_reporting.runtime.review_preflight import (
    evaluate_module_review_preflight,
)


def _deep_text(submodule_id: str, title: str) -> str:
    return (
        f"{submodule_id} {title} 已批准专业分析正文。现有条件可能导致相关风险沿上下游传播，"
        "其影响会在负荷变化或维护不足时进一步放大。建议结合运行记录持续监测，"
        "对关键假设开展专项核验，并以整改复测结果验证风险是否得到控制。"
    )


def _organized_conclusion() -> str:
    return (
        "综合五个专业模块，系统整体具备基本运行条件，但风险并非由单一设备问题构成，"
        "而是由系统边界、环境压力、保护策略、设备状态和运维能力共同决定。当前风险簇应按"
        "影响范围、发生可能性和故障传播能力分级，其中会扩大停电范围或削弱保护选择性的事项应优先处理。"
        "整改行动应先确认供电与保护边界，再处理高风险设备缺陷和环境问题，最后通过运维制度、人员责任和"
        "数字化监测固化成果。由于部分数据仍存在不足和待核验边界，后续应补充动态负荷、动作记录和复测指标；"
        "在这些限制关闭前，相关判断保持审慎，不把通用经验替代项目事实。"
        "管理层还应按月检查行动包进度、跨部门接口和验证记录，确保优先事项完成后再关闭风险。"
    )


def _edited(**updates) -> EditedReportSubmission:
    synthesis = _organized_conclusion() * 2
    values = {
        "title": "配电安全专家咨询报告",
        "assessment_background": synthesis,
        "findings_overview": synthesis,
        "regional_executive_summary": synthesis,
        "module_narratives": {
            module_id: f"模块 {module_id} 正文" for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
        },
        "risk_panorama": synthesis,
        "dimension_risk_analysis": (
            synthesis
            + "系统架构、电能质量、保护、设备与运维之间存在共同前提、叠加影响和传播关系。"
        ),
        "data_gap_analysis": (
            synthesis + "数据不足会限制判断并影响置信度，因此应优先补证。"
        ),
        "improvement_action_plan": (
            synthesis + "由责任部门牵头，按依赖和优先级分阶段实施，并以指标、复测和验收关闭。"
        ),
        "special_topic_plan": {
            "source_ref": "Inputs/专项问题分析.md",
            "source_sha256": "0" * 64,
            "sections": [
                {
                    "section_id": "4.1",
                    "title": "动态专项问题",
                    "requirement": "分析项目边界、可选方案和验证方法。",
                }
            ],
        },
        "special_topic_analysis": (
            "### 4.1 动态专项问题\n\n"
            + synthesis
            + "结合项目边界比较可选方案，并说明验证方法。"
        ),
    }
    values.update(updates)
    return EditedReportSubmission(**values)


def _claim() -> ClaimRecord:
    return ClaimRecord(
        id="C-2.4-001",
        module_id="2.4",
        submodule_id="2.4.2.3",
        text="1A2 柜连接状态需要复核",
        claim_type="risk_judgment",
        source_ids=["E-0001"],
    )


def _approved_modules() -> dict[str, ModuleSubmission]:
    return {
        module_id: ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                submodule_id: _deep_text(submodule_id, submodule.title)
                for submodule_id, submodule in definition.submodules.items()
            },
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )
        for module_id, definition in REPORT_TAXONOMY.items()
    }


def test_report_taxonomy_matches_s4_6_columns_b_to_e() -> None:
    expected = {
        "2.1": (
            "配电系统架构问题",
            (
                ("2.1.1", "配电系统负荷分配与过载风险"),
                ("2.1.2", "关键负荷供电路径与应急/备用供电的问题"),
                ("2.1.3", "配网自动化、备用电源自动切换（可能性及功能验证）"),
                ("2.1.4", "防止2路电源并联产生环流"),
                ("2.1.5", "系统无功补偿与电容柜问题"),
            ),
        ),
        "2.2": (
            "环境工况风险",
            (
                ("2.2.1", "来自电能质量的风险"),
                ("2.2.1.1", "谐波风险情况"),
                ("2.2.1.2", "电压扰动情况"),
                ("2.2.1.3", "频繁启动与冲击负荷"),
                ("2.2.2", "其他运行工况风险"),
                ("2.2.2.1", "低压配电设备发热情况"),
                ("2.2.2.2", "高压配电设备局放情况"),
                ("2.2.2.3", "其他物理环境风险"),
            ),
        ),
        "2.3": (
            "针对故障的保护",
            (
                ("2.3.1", "配电系统保护方案与定值的论证计算"),
                ("2.3.2", "零序/漏电的防范"),
                ("2.3.3", "电压事件（过压）的防范"),
            ),
        ),
        "2.4": (
            "配电设备/元件内在风险",
            (
                ("2.4.1", "配置与选型问题"),
                ("2.4.1.1", "额定/分断能力"),
                ("2.4.1.2", "配电柜分隔形式"),
                ("2.4.1.3", "配电设备安全连锁/闭锁"),
                ("2.4.1.4", "设备分合/储能/工作位置显示"),
                ("2.4.2", "安装规范性问题"),
                ("2.4.2.1", "裸露导体防护"),
                ("2.4.2.2", "等电位连接与接地问题"),
                ("2.4.2.3", "电气连接问题"),
                ("2.4.2.4", "标牌标识"),
                ("2.4.2.5", "电缆、桥架、母线安装问题"),
                ("2.4.2.6", "设备外壳IP等级与封堵问题"),
                ("2.4.3", "带病运行问题汇总"),
                ("2.4.3.1", "低压回路剩余电流过大"),
                ("2.4.3.2", "部分高压柜照明功能缺失"),
                ("2.4.3.3", "部分高压柜柜内除湿装置未开启"),
                ("2.4.4", "末端配电抽查情况"),
            ),
        ),
        "2.5": (
            "运维管理与风险管控机制",
            (
                ("2.5.1", "SOP/EOP"),
                ("2.5.2", "图纸资料"),
                ("2.5.3", "运维（巡检、维护）的实施与组织"),
                ("2.5.3.1", "运维组织架构与人员配备"),
                ("2.5.3.2", "关键配电设备维护工作全面性检查"),
                ("2.5.3.3", "配电设备维保覆盖"),
                ("2.5.4", "运维的智能化手段"),
                ("2.5.5", "配电室装备与LOTO流程的实施"),
                ("2.5.6", "备件管理"),
                ("2.5.7", "退市设备与生命周期管理"),
            ),
        ),
    }

    actual = {
        module_id: (
            module.title,
            tuple(
                (section_id, section.title)
                for section_id, section in module.sections.items()
            ),
        )
        for module_id, module in REPORT_TAXONOMY.items()
    }
    assert actual == expected


def test_module_markdown_preserves_intermediate_s4_6_headings() -> None:
    module = REPORT_TAXONOMY["2.4"]
    markdown = compose_module_markdown(
        "2.4",
        {
            submodule_id: _deep_text(submodule_id, submodule.title)
            for submodule_id, submodule in module.submodules.items()
        },
    )

    assert "### 2.4.1 配置与选型问题" in markdown
    assert "#### 2.4.1.1 额定/分断能力" in markdown
    assert "### 2.4.2 安装规范性问题" in markdown
    assert "### 2.4.3 带病运行问题汇总" in markdown
    assert markdown.index("### 2.4.1 配置与选型问题") < markdown.index(
        "#### 2.4.1.1 额定/分断能力"
    )


def _complete_markdown_modules() -> dict[str, str]:
    return {
        module_id: compose_module_markdown(
            module_id,
            {
                submodule_id: (
                    "**现状描述：** 已核查现场记录、运行数据及文件资料，并明确了证据适用边界。\n\n"
                    "**判断：** 当前情况表明该项可能影响系统可靠性，仍需结合持续数据复核。\n\n"
                    "**风险与影响：** 若运行条件恶化，问题可能沿上下游扩大并导致供电中断。\n\n"
                    "**建议：** 应由责任部门完成专项排查、整改和复测，并以验收记录关闭风险。"
                )
                for submodule_id in definition.submodules
            },
        )
        for module_id, definition in REPORT_TAXONOMY.items()
    }


def _quality_edited() -> EditedReportSubmission:
    return _edited(
        module_narratives={
            module_id: "\n\n".join(
                f"### {submodule_id} {submodule.title}\n"
                f"{_deep_text(submodule_id, submodule.title)}"
                for submodule_id, submodule in definition.submodules.items()
            )
            for module_id, definition in REPORT_TAXONOMY.items()
        }
    )


def test_existing_markdown_modules_require_every_full_submodule() -> None:
    modules = _complete_markdown_modules()
    modules["2.5"] = (
        "## 2.5 运维管理与风险管控机制\n\n"
        "九个子模块的结论概述见汇总表，仅引用 E-0001 至 E-0009。"
    )

    with pytest.raises(ValueError, match="missing fixed sections"):
        validate_existing_markdown_modules(modules)


def test_existing_markdown_modules_accept_detailed_fixed_sections() -> None:
    validate_existing_markdown_modules(_complete_markdown_modules())


def test_module_markdown_audit_rejects_export_that_drops_fixed_sections() -> None:
    modules = _approved_modules()
    exported = {module_id: module.markdown for module_id, module in modules.items()}
    exported["2.5"] = "## 2.5 运维管理与风险管控机制\n\n九个子模块概览见汇总表。"

    with pytest.raises(ValueError, match="missing_export_sections.*2.5.1"):
        validate_module_markdown_consistency(modules, exported)


def test_module_preflight_does_not_gate_on_file_hash(tmp_path: Path) -> None:
    module = _approved_modules()["2.1"]
    subject_ref = "Work/runs/run-preflight/modules/2.1-r0.json"
    subject_path = tmp_path / subject_ref
    subject_path.parent.mkdir(parents=True)
    subject_path.write_text(module.model_dump_json(), encoding="utf-8")
    upstream = ValidationReport(
        validation_protocol_version=2,
        run_id="run-preflight",
        subject_ref=subject_ref,
        subject_revision=0,
        validator="module-structure/v2",
        check_ids=["module.canonical_markdown"],
        failures=[],
        passed=True,
    )

    result = evaluate_module_review_preflight(
        tmp_path,
        run_id="run-preflight",
        subject=module,
        subject_ref=subject_ref,
        upstream_report=upstream,
    )

    assert result.report.passed is True
    assert "content_sha256" not in type(result.report).model_fields


def test_editor_must_protect_every_approved_claim() -> None:
    with pytest.raises(ValueError, match="protected claim"):
        validate_editor_protection(_edited(), [_claim()])


def test_editor_claim_marker_must_occur_once_in_claim_module() -> None:
    edited = _edited(
        protected_claim_ids=["C-2.4-001"],
        module_narratives={
            "2.1": "模块 2.1 正文",
            "2.2": "模块 2.2 正文",
            "2.3": "模块 2.3 正文",
            "2.4": "[[CLAIM:C-2.4-001]]；[[CLAIM:C-2.4-001]]",
            "2.5": "模块 2.5 正文",
        },
    )

    with pytest.raises(ValueError, match="exactly once"):
        validate_editor_protection(edited, [_claim()])


def test_editor_claim_marker_must_stay_in_its_fixed_submodule() -> None:
    claim = _claim()
    edited = _edited(
        protected_claim_ids=[claim.id],
        module_narratives={
            **_edited().module_narratives,
            "2.4": (
                "#### 2.4.2.2 等电位连接与接地问题\n"
                "错误子模块承载了引用 [[CLAIM:C-2.4-001]]。\n\n"
                "#### 2.4.2.3 电气连接问题\n"
                "正确子模块正文。"
            ),
        },
    )

    with pytest.raises(ValueError, match="must remain in submodule"):
        validate_editor_protection(edited, [claim])


def test_editor_protection_rejects_noncanonical_claim_source_ids() -> None:
    claim = _claim().model_copy(update={"source_ids": ["source-table-row-1"]})
    edited = _edited(
        protected_claim_ids=[claim.id],
        module_narratives={
            **_edited().module_narratives,
            "2.4": "正文 [[CLAIM:C-2.4-001]]",
        },
    )

    with pytest.raises(ValueError, match="non-canonical source ids"):
        validate_editor_protection(edited, [claim])


def test_module_claim_marker_is_owned_by_the_claim_submodule() -> None:
    claim = _claim()
    modules = _approved_modules()
    modules["2.4"] = ModuleSubmission.model_validate(
        {
            **modules["2.4"].model_dump(mode="python"),
            "claims": [claim],
            "source_ids": ["E-0001"],
            "submodule_narratives": {
                **modules["2.4"].submodule_narratives,
                claim.submodule_id: (
                    "现场记录确认 1A2 柜的连接状态仍需安排停电复核。"
                    "[[CLAIM:C-2.4-001]]"
                    "整改完成后应进行温升复测并归档。"
                ),
            },
        }
    )
    edited = _edited(
        protected_claim_ids=[claim.id],
        module_narratives={
            **_edited().module_narratives,
            "2.4": compose_module_markdown(
                "2.4", modules["2.4"].submodule_narratives
            ),
        },
    )

    validate_editor_protection(edited, [claim])


def test_module_submission_rejects_unknown_claim_marker() -> None:
    claim = _claim()
    modules = _approved_modules()
    with pytest.raises(ValueError, match="unknown Claim markers"):
        ModuleSubmission.model_validate(
            {
                **modules["2.4"].model_dump(mode="python"),
            "claims": [claim],
                "source_ids": ["E-0001"],
            "submodule_narratives": {
                **modules["2.4"].submodule_narratives,
                    claim.submodule_id: "天气晴朗。[[CLAIM:C-UNKNOWN]]",
                },
            },
        )


def test_editor_quality_requires_every_fixed_submodule() -> None:
    edited = _quality_edited()
    narratives = dict(edited.module_narratives)
    narratives["2.1"] = narratives["2.1"].replace("2.1.5 系统无功补偿与电容柜问题", "")

    with pytest.raises(ValueError, match="omitted fixed submodules"):
        validate_editor_quality(
            edited.model_copy(update={"module_narratives": narratives}),
            _approved_modules(),
        )


def test_editor_quality_does_not_compare_approved_prose_bytes() -> None:
    edited = _quality_edited()
    modules = _approved_modules()
    modules["2.1"] = modules["2.1"].model_copy(
        update={
            "submodule_narratives": {
                key: value + ("专业机理、风险后果和整改验收要求。" * 20)
                for key, value in modules["2.1"].submodule_narratives.items()
            }
        }
    )

    assert validate_editor_quality(edited, modules) == []


def test_editor_quality_compares_prose_after_canonical_heading_normalization() -> None:
    modules = _approved_modules()
    narratives = dict(modules["2.5"].submodule_narratives)
    narratives["2.5.1"] = (
        "### 2.5.1 模型提交的扩展标题\n\n" + narratives["2.5.1"]
    )
    modules["2.5"] = ModuleSubmission(
        **{
            **modules["2.5"].model_dump(mode="python"),
            "submodule_narratives": narratives,
        }
    )
    edited = _quality_edited().model_copy(
        update={
            "module_narratives": {
                **_quality_edited().module_narratives,
                "2.5": modules["2.5"].markdown,
            }
        }
    )

    validate_editor_quality(edited, modules)


def test_editor_quality_does_not_reaudit_approved_submodule_semantics() -> None:
    modules = _approved_modules()
    narratives = dict(modules["2.1"].submodule_narratives)
    narratives["2.1.1"] = "发现问题，建议整改。"
    modules["2.1"] = ModuleSubmission.model_validate(
        {
            **modules["2.1"].model_dump(mode="python"),
            "submodule_narratives": narratives,
        }
    )
    edited = _quality_edited().model_copy(
        update={
            "module_narratives": {
                **_quality_edited().module_narratives,
                "2.1": modules["2.1"].markdown,
            }
        }
    )

    validate_editor_quality(edited, modules)


def test_editor_quality_does_not_score_short_risk_panorama() -> None:
    edited = _quality_edited().model_copy(update={"risk_panorama": "建议后续整改。"})

    observations = validate_editor_quality(edited, _approved_modules())
    assert observations == []


def test_editor_quality_skips_optional_special_topic_gate_when_plan_is_absent() -> None:
    edited = _quality_edited().model_copy(
        update={"special_topic_plan": None, "special_topic_analysis": None}
    )

    observations = validate_editor_quality(edited, _approved_modules())

    assert not any(item.startswith("special_topic_analysis:") for item in observations)


def test_editor_quality_does_not_score_pointer_only_sections() -> None:
    edited = _quality_edited().model_copy(
        update={
            "assessment_background": "评估背景详见第二章。",
            "findings_overview": "关键发现见2.1至2.5。",
            "regional_executive_summary": "区域重点见第二章。",
            "risk_panorama": "总体风险详见各模块。" * 30,
            "dimension_risk_analysis": "各维度风险判断见第二章。",
            "data_gap_analysis": "数据缺口见各模块证据限定。",
            "improvement_action_plan": "整改建议见各模块原文。",
            "special_topic_analysis": (
                "### 4.1 动态专项问题\n\n专项分析建议见前文。"
            ),
        }
    )

    observations = validate_editor_quality(edited, _approved_modules())
    assert observations == []


def test_asset_assembler_builds_traceable_table_and_photo(tmp_path: Path) -> None:
    photo_path = tmp_path / "Work/assets/IMG-1.png"
    photo_path.parent.mkdir(parents=True)
    photo_path.write_bytes(b"image")
    claim = _claim()
    evidence = EvidenceItem(
        id="E-0001",
        subject="1A2 柜",
        fact="连接点存在异常",
        source=SourceLocation(file_id="F-1", path=Path("Inputs/check.xlsx"), cell="A2"),
        module_id="2.4",
        submodule_id="2.4.2.3",
        photo_refs=["IMG-1"],
    )
    asset = PhotoAsset(
        id="IMG-1",
        path=photo_path.relative_to(tmp_path),
        sha256="abc",
        media_type="image/png",
        source_member="media/image1.png",
    )
    edited = _edited(
        protected_claim_ids=[claim.id],
        module_narratives={
            "2.1": "模块 2.1 正文",
            "2.2": "模块 2.2 正文",
            "2.3": "模块 2.3 正文",
            "2.4": "1A2 柜连接状态需要复核[[CLAIM:C-2.4-001]]",
            "2.5": "模块 2.5 正文",
        },
        photo_ids=["IMG-1"],
        tables=[
            TableSubmission(
                title="整改行动表",
                headers=["对象", "动作"],
                rows=[["1A2", "复核连接"]],
                source_ids=["E-0001"],
                claim_ids=[claim.id],
            )
        ],
    )

    tables, photos = ReportAssetAssembler(tmp_path).build([evidence], [asset], [claim], edited)

    assert tables[0].source_ids == ["E-0001"]
    assert photos[0].path == photo_path
    assert photos[0].source_id == "E-0001"
    assert photos[0].claim_ids == [claim.id]
    assert photos[0].submodule_id == "2.4.2.3"


def test_asset_assembler_requires_and_builds_every_source_table_photo(
    tmp_path: Path,
) -> None:
    paths = [
        tmp_path / "Work/assets/IMG-1.png",
        tmp_path / "Work/assets/IMG-2.png",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")
    claim = _claim()
    evidence = [
        EvidenceItem(
            id="E-0001",
            subject="1A2 柜",
            fact="连接点存在异常",
            source=SourceLocation(
                file_id="F-1",
                path=Path("Inputs/check.xlsx"),
                cell="A2",
            ),
            module_id="2.4",
            submodule_id="2.4.2.3",
            photo_refs=["IMG-1"],
        ),
        EvidenceItem(
            id="E-0002",
            subject="缺陷判定标准",
            fact="原表包含判定标准图",
            source=SourceLocation(
                file_id="F-1",
                path=Path("Inputs/check.xlsx"),
                cell="J1",
            ),
            module_id="2.2",
            submodule_id="2.2.2.1",
            photo_refs=["IMG-2"],
        ),
    ]
    assets = [
        PhotoAsset(
            id=f"IMG-{index}",
            path=path.relative_to(tmp_path),
            sha256=str(index),
            media_type="image/png",
            source_member=f"media/image{index}.png",
        )
        for index, path in enumerate(paths, start=1)
    ]
    edited = _edited(photo_ids=["IMG-1", "IMG-2"])

    _, photos = ReportAssetAssembler(tmp_path).build(
        evidence,
        assets,
        [claim],
        edited,
    )

    assert [photo.id for photo in photos] == ["IMG-1", "IMG-2"]
    assert photos[1].claim_ids == []
    assert photos[1].submodule_id == "2.2.2.1"

    with pytest.raises(ValueError, match="runtime-owned source-table photo set"):
        ReportAssetAssembler(tmp_path).build(
            evidence,
            assets,
            [claim],
            edited.model_copy(update={"photo_ids": ["IMG-1"]}),
        )


def test_asset_assembler_rejects_source_table_photo_without_runtime_asset(
    tmp_path: Path,
) -> None:
    evidence = EvidenceItem(
        id="E-0001",
        subject="对象",
        fact="检查结果=NG",
        source=SourceLocation(
            file_id="F-1",
            path=Path("Inputs/check.xlsx"),
            cell="A1",
        ),
        module_id="2.4",
        submodule_id="2.4.2.1",
        photo_refs=["P-MISSING"],
    )

    with pytest.raises(ValueError, match="missing from the runtime manifest"):
        runtime_photo_ids([evidence], [])


def test_asset_assembler_rejects_non_e_photo_evidence_binding() -> None:
    evidence = EvidenceItem(
        id="row-1",
        subject="对象",
        fact="检查结果=NG",
        source=SourceLocation(
            file_id="F-1",
            path=Path("Inputs/check.xlsx"),
            cell="A1",
        ),
        module_id="2.4",
        submodule_id="2.4.2.1",
        photo_refs=["P-0001"],
    )
    asset = PhotoAsset(
        id="P-0001",
        path=Path("Work/assets/P-0001.png"),
        sha256="abc",
        media_type="image/png",
        source_member="media/image1.png",
    )

    with pytest.raises(ValueError, match=r"canonical E-\* ids"):
        runtime_photo_ids([evidence], [asset])


def test_asset_assembler_rejects_unknown_explicit_primary_photo_evidence(
    tmp_path: Path,
) -> None:
    photo_path = tmp_path / "Work/assets/P-0001.png"
    photo_path.parent.mkdir(parents=True)
    photo_path.write_bytes(b"image")
    evidence = [
        EvidenceItem(
            id=f"E-000{index}",
            subject=f"对象 {index}",
            fact="检查结果=NG",
            source=SourceLocation(
                file_id="F-1",
                path=Path("Inputs/check.xlsx"),
                cell=f"A{index}",
            ),
            module_id="2.4",
            submodule_id="2.4.2.1",
            photo_refs=["P-0001"],
        )
        for index in (1, 2)
    ]
    asset = PhotoAsset(
        id="P-0001",
        path=photo_path.relative_to(tmp_path),
        sha256="abc",
        media_type="image/png",
        source_member="media/image1.png",
        source_image_id="ID_SOURCE",
        primary_evidence_id="E-9999",
    )

    with pytest.raises(ValueError, match="primary evidence binding"):
        runtime_photo_ids(evidence, [asset])


def test_asset_assembler_uses_explicit_primary_binding_for_reused_photo(
    tmp_path: Path,
) -> None:
    photo_path = tmp_path / "Work/assets/P-0001.png"
    photo_path.parent.mkdir(parents=True)
    photo_path.write_bytes(b"image")
    evidence = [
        EvidenceItem(
            id=f"E-000{index}",
            subject=f"对象 {index}",
            fact=f"检查结果 {index}=NG",
            source=SourceLocation(
                file_id="F-1",
                path=Path("Inputs/check.xlsx"),
                cell=f"A{index}:B{index}",
            ),
            module_id="2.4",
            submodule_id="2.4.2.1",
            photo_refs=["P-0001"],
        )
        for index in (1, 2)
    ]
    asset = PhotoAsset(
        id="P-0001",
        path=photo_path.relative_to(tmp_path),
        sha256="abc",
        media_type="image/png",
        source_member="media/image1.png",
        source_image_id="ID_SOURCE",
        primary_evidence_id="E-0002",
    )

    _, photos = ReportAssetAssembler(tmp_path).build(
        evidence,
        [asset],
        [],
        _edited(photo_ids=["P-0001"]),
    )

    assert photos[0].source_id == "E-0002"
    assert photos[0].caption == "对象 2：检查结果 2=NG"
