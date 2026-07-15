# Phase A V2 业务纵向迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 AutoReport 当前项目内完成三表解析、2.4 证据链、确定性覆盖、真实模块写作/审校返工和 DOCX 交付的 Phase A 纵向样板。

**Architecture:** 将交接包 0.2.8 单体解析与渲染行为拆为 intake adapters、domain mappers、coverage、workers、review 和 renderer；所有状态通过严格 Pydantic 载体进入项目 `Work/`/`Outputs/`。AutoReport 的 Main Agent、MessageBus 和 TaskBoard 是唯一运行入口。

**Tech Stack:** Python 3.12、Pydantic 2、OpenPyXL、python-docx、PyYAML、PyQt6、pytest、pytest-asyncio。

## Global Constraints

- `work/` 只读；测试先复制资料到仓库内 `.build-tmp/` 或 pytest 项目目录。
- 以 `pds_report_tools_v3_dynamic-0.2.8` 的行为和当前知识库为迁移基线。
- 固定 taxonomy 不交给模型修改。
- 客户事实只能来自 EvidenceItem；KU/Skill 只能提供判断方法。
- 每个新行为先看到预期失败，再写生产代码。
- Phase A 只完成 2.4 业务样板，但数据载体与编排接口必须可扩展到 2.1-2.5。

---

### Task 1: 固定 taxonomy 与业务载体

**Files:**
- Create: `autoreport/core/reporting/taxonomy.py`
- Modify: `autoreport/core/reporting/models.py`
- Test: `tests/reporting/test_taxonomy.py`
- Test: `tests/reporting/test_models.py`

**Interfaces:**
- Produces: `REPORT_TAXONOMY`, `EvidenceItem.module_id/submodule_id/photo_refs`, `Claim`, `ReportState`.
- Consumes: existing `SourceLocation`, `CoverageMatrix`, `ModuleDraft`.

- [ ] **Step 1: Write failing taxonomy and carrier tests**

```python
def test_taxonomy_contains_fixed_24_submodules():
    assert "2.4.2.2" in REPORT_TAXONOMY["2.4"].submodules

def test_claim_requires_traceable_evidence_for_quantitative_fact():
    with pytest.raises(ValidationError):
        Claim(module_id="2.4", submodule_id="2.4.3.1", text="剩余电流为46.8A", evidence_ids=[])
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/pytest tests/reporting/test_taxonomy.py tests/reporting/test_models.py -q`

Expected: imports for `REPORT_TAXONOMY` and `Claim` fail.

- [ ] **Step 3: Implement strict types and fixed taxonomy**

Implement immutable module/submodule metadata and validators that reject unknown IDs and evidence-free quantitative claims.

- [ ] **Step 4: Run GREEN and the reporting suite**

Run: `.venv/bin/pytest tests/reporting -q`

- [ ] **Step 5: Commit**

Commit: `feat: define report taxonomy and claim contracts`

### Task 2: 建立 Manifest 与核心工作表识别

**Files:**
- Create: `autoreport/core/reporting/intake/manifest.py`
- Create: `autoreport/core/reporting/intake/workbook.py`
- Modify: `autoreport/core/reporting/service.py`
- Test: `tests/reporting/intake/test_manifest.py`
- Test: `tests/reporting/intake/test_workbook.py`

**Interfaces:**
- Produces: `build_manifest(workspace) -> ProjectManifest`, `WorkbookArtifact` with sheet/cell/formula metadata.
- Consumes: project `Inputs/` only.

- [ ] **Step 1: Write failing tests for purpose detection and ignored paths**

```python
def test_manifest_classifies_three_core_workbooks(project_with_core_files):
    manifest = build_manifest(project_with_core_files)
    assert {item.purpose for item in manifest.files} == {"s2-1", "s4-4", "s4-6"}
```

- [ ] **Step 2: Run RED and confirm missing intake package**
- [ ] **Step 3: Implement stable IDs, SHA256, media type, purpose and parse status**
- [ ] **Step 4: Verify only `Inputs/` is scanned and one corrupt workbook does not abort others**
- [ ] **Step 5: Run GREEN and commit `feat: add report intake manifest`**

### Task 3: 迁移 S2-1、S4-4、S4-6 专用映射与 DISPIMG

**Files:**
- Create: `autoreport/core/reporting/intake/wps_images.py`
- Create: `autoreport/core/reporting/mappers/s2_1.py`
- Create: `autoreport/core/reporting/mappers/s4_4.py`
- Create: `autoreport/core/reporting/mappers/s4_6.py`
- Create: `autoreport/core/reporting/mappers/common.py`
- Test: `tests/reporting/mappers/test_s2_1.py`
- Test: `tests/reporting/mappers/test_s4_4.py`
- Test: `tests/reporting/mappers/test_s4_6.py`
- Test: `tests/reporting/intake/test_wps_images.py`

**Interfaces:**
- Produces: `map_s2_1()`, `map_s4_4()`, `map_s4_6()` returning `EvidenceItem[]` and gaps.
- Produces: `extract_wps_images(path) -> dict[str, PhotoAsset]` keyed by DISPIMG ID.

- [ ] **Step 1: Add RED tests using read-only real workbook paths**

```python
def test_s4_4_maps_low_voltage_issue_to_same_row_photo(real_s4_4):
    evidence = map_s4_4(real_s4_4)
    item = next(x for x in evidence if x.subject == "车间配电房/1A2" and "电缆" in x.fact)
    assert item.submodule_id == "2.4.2.1"
    assert item.photo_refs
    assert item.source.sheet == "低配评估详情"
```

- [ ] **Step 2: Run each test and confirm it fails for the missing mapper**
- [ ] **Step 3: Implement WPS XML relation traversal and media extraction into project `Work/assets/`**
- [ ] **Step 4: Implement S2-1 availability/effectiveness evidence**
- [ ] **Step 5: Implement S4-4 paired status/photo columns, low-voltage details and thermal rows**
- [ ] **Step 6: Implement S4-6 taxonomy/summary mapping without treating filenames as locations**
- [ ] **Step 7: Add regression for 96.99% load rate classification**
- [ ] **Step 8: Run GREEN and commit `feat: map core customer workbooks to evidence`**

### Task 4: 确定性 Coverage 与 2.4 Planner

**Files:**
- Create: `autoreport/core/reporting/coverage.py`
- Create: `autoreport/core/reporting/planner.py`
- Modify: `autoreport/core/reporting/service.py`
- Test: `tests/reporting/test_coverage.py`
- Test: `tests/reporting/test_planner.py`

**Interfaces:**
- Produces: `evaluate_coverage(request, evidence) -> CoverageMatrix` at submodule level.
- Produces: `plan_modules(request, coverage, evidence) -> list[ModuleTask]`.

- [ ] **Step 1: Write RED tests proving unrelated evidence cannot make 2.4 ready**
- [ ] **Step 2: Write RED tests for ask/skip/draft/block policies**
- [ ] **Step 3: Implement minimum evidence rules per 2.4 submodule**
- [ ] **Step 4: Ensure pending gaps flow into ModuleTask and blocked tasks stop before drafting**
- [ ] **Step 5: Run GREEN and commit `feat: add deterministic submodule coverage`**

### Task 5: 实现真实 2.4 Worker 与 claim 输出

**Files:**
- Create: `autoreport/core/reporting/skills/resolver.py`
- Create: `autoreport/core/reporting/workers/base.py`
- Create: `autoreport/core/reporting/workers/module_24.py`
- Create: `autoreport/templates/reporting/skills/2.4/*.md`
- Modify: `autoreport/core/reporting/service.py`
- Test: `tests/reporting/workers/test_module_24.py`

**Interfaces:**
- Produces: `Module24Worker.run(task, evidence, skills) -> ModuleDraft` containing typed `Claim[]`.
- Consumes: only evidence IDs assigned by Planner and approved 2.4 skills.

- [ ] **Step 1: Write RED test for fact -> mechanism -> risk -> recommendation claims**
- [ ] **Step 2: Write RED test preventing 96.99% from becoming an existing >100% overload/fire claim**
- [ ] **Step 3: Implement deterministic rule layer first, then optional LLM prose adapter**
- [ ] **Step 4: Persist skill IDs and versions on every claim**
- [ ] **Step 5: Run GREEN and commit `feat: add evidence-bound module 2.4 worker`**

### Task 6: Evidence Auditor 与局部返工

**Files:**
- Create: `autoreport/core/reporting/review/auditor.py`
- Create: `autoreport/core/reporting/review/revisions.py`
- Modify: `autoreport/core/reporting/service.py`
- Test: `tests/reporting/review/test_auditor.py`
- Test: `tests/reporting/review/test_revisions.py`

**Interfaces:**
- Produces: `audit_draft(draft, evidence, skills) -> list[ReviewIssue]`.
- Produces: `RevisionRouter(max_rounds=2).route(...)` for the responsible submodule only.

- [ ] **Step 1: Write RED tests for missing evidence, wrong source, threshold misuse and missing photo state**
- [ ] **Step 2: Write RED test that a 2.4.2.2 revision leaves 2.4.2.1 unchanged**
- [ ] **Step 3: Implement structural checks before semantic checks**
- [ ] **Step 4: Implement two-round limit and user escalation payload**
- [ ] **Step 5: Run GREEN and commit `feat: audit claims and route local revisions`**

### Task 7: ReportState 与本地 DOCX Renderer

**Files:**
- Create: `autoreport/core/reporting/report_state.py`
- Create: `autoreport/core/reporting/rendering/docx.py`
- Add: `autoreport/templates/reporting/report_template.docx`
- Modify: `autoreport/core/reporting/service.py`
- Test: `tests/reporting/rendering/test_docx.py`
- Test: `tests/reporting/test_report_state.py`

**Interfaces:**
- Produces: `build_report_state(...) -> ReportState`.
- Produces: `DocxRenderer.render(state, output_path) -> RenderResult`.

- [ ] **Step 1: Write RED structural tests for headings, issue table, image fallback and output path**
- [ ] **Step 2: Copy the approved template into packaged resources and record its SHA256**
- [ ] **Step 3: Implement renderer as a pure ReportState consumer**
- [ ] **Step 4: Add pre/post render validation and render log**
- [ ] **Step 5: Run GREEN and commit `feat: render audited report state to docx`**

### Task 8: 真实资料端到端与视觉验收

**Files:**
- Create: `tests/reporting/integration/test_real_phase_a_24.py`
- Create: `tests/reporting/fixtures/README.md`
- Modify: `README.md`
- Modify: `README_zh.md`

**Interfaces:**
- Verifies: Main Agent tool -> three workbooks -> Evidence -> Coverage -> 2.4 -> Audit -> DOCX.

- [ ] **Step 1: Write the end-to-end test and verify it fails before final wiring**
- [ ] **Step 2: Wire the complete service flow and make the test pass**
- [ ] **Step 3: Run reporting tests, full suite, Ruff and wheel build**
- [ ] **Step 4: Render the generated DOCX to PNG and inspect every page**
- [ ] **Step 5: Check HTML Phase A and handoff constraints line by line; save an acceptance matrix**
- [ ] **Step 6: Commit `feat: complete phase a module 2.4 vertical slice`**

## Phase A Completion Gate

Phase A is complete only when all eight tasks are committed, all tests and build checks pass, a real DOCX is generated from the three supplied workbooks, every rendered page is visually reviewed, and the acceptance matrix contains no missing Phase A requirement.
