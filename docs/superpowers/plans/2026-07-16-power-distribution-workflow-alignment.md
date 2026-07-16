# Power Distribution Workflow Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the active AutoReport workflow honor the approved request, Knowledge, submodule, asset, rendering, state, and capability-governance contracts without restoring the deleted deterministic writer.

**Architecture:** Keep `ReportWorkflowRunner` as the explicit Python control plane, but move policies into focused components: a name-agnostic Knowledge library, packaged DOCX core, request gate, module Skill loader, submodule-preserving revision validator, asset assembler, intake adapter registry, and project-local Skill governance store. YAML remains the declarative source for agent identities, pipeline ownership, and revision budgets.

**Tech Stack:** Python 3.12, Pydantic 2, asyncio, PyYAML, openpyxl, python-docx, Pillow, pytest, pytest-asyncio, Ruff.

## Global Constraints

- Work in the existing main checkout because the approved cleanup is already uncommitted there; do not create a separate worktree.
- Runtime reference access is bounded only by `<project>/Knowledge`; do not match `01` or `02` directory names.
- Keep E-* project evidence, R-* Knowledge references, and W-* web references semantically distinct.
- Production DOCX generation must use packaged code and files; explicit injected renderer paths are test-only overrides.
- Do not add a new GUI page or restore the deterministic professional-writing pipeline.
- Every production behavior starts with a failing regression test and completes with a green focused suite.

---

### Task 1: Name-agnostic Knowledge boundary

**Files:**
- Modify: `autoreport/core/reporting/research/reference_library.py`
- Modify: `autoreport/core/reporting/source_ledger.py`
- Modify: `autoreport/core/reporting/claim_ledger.py`
- Modify: `autoreport/core/reporting/agent_runner.py`
- Modify: `autoreport/core/tools/reporting_research_tools.py`
- Test: `tests/reporting/research/test_reference_library.py`
- Test: `tests/reporting/test_source_ledger.py`
- Test: `tests/reporting/test_claim_ledger.py`
- Test: `tests/reporting/test_agent_runner.py`

**Interfaces:**
- Produces: `ReferenceLibrary(workspace).root == workspace / "Knowledge"` and safe recursive `search()` / `open()`.
- Produces: `SourceLedger.register_local(title, "Knowledge/<any path>", content) -> SourceRecord`.

- [ ] **Step 1: Replace 01/02 fixtures with arbitrary Knowledge paths and add escape tests**

```python
def test_searches_every_supported_file_beneath_knowledge(tmp_path: Path) -> None:
    nested = tmp_path / "Knowledge/任意目录/标准"
    nested.mkdir(parents=True)
    (nested / "接地.md").write_text("保护接地连续性", encoding="utf-8")
    hits = ReferenceLibrary(tmp_path).search("接地")
    assert [hit.relative_path for hit in hits] == ["Knowledge/任意目录/标准/接地.md"]

def test_rejects_paths_outside_knowledge(tmp_path: Path) -> None:
    outside = tmp_path / "Inputs/secret.md"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="Knowledge"):
        ReferenceLibrary(tmp_path).open("Inputs/secret.md")
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/research/test_reference_library.py tests/reporting/test_source_ledger.py tests/reporting/test_claim_ledger.py -q`

Expected: failures reference the fixed `Knowledge/01_页面导入知识库` root or prefix.

- [ ] **Step 3: Implement path-containment checks without name matching**

```python
self.root = (self.workspace / "Knowledge").resolve()

def _safe_path(self, value: str | Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = self.workspace / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(self.root):
        raise ValueError("reference path must stay beneath project Knowledge")
    return resolved
```

Change local-source validation to `normalized.startswith("Knowledge/")` and remove all 01/02 string checks and user-facing descriptions.

- [ ] **Step 4: Run focused tests and source scan**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/research/test_reference_library.py tests/reporting/test_source_ledger.py tests/reporting/test_claim_ledger.py tests/reporting/test_agent_runner.py tests/reporting/test_reporting_research_tools.py -q`

Run: `rg -n '01_页面导入知识库|02_本地skill提示词资料_禁止导入' autoreport/core autoreport/templates`

Expected: tests pass; scan has no runtime contract matches.

### Task 2: Packaged DOCX rendering core

**Files:**
- Create: `autoreport/core/reporting/rendering/packaged_docx.py`
- Modify: `autoreport/core/reporting/workflow.py`
- Modify: `autoreport/core/reporting/rendering/__init__.py`
- Modify: `tests/reporting/rendering/test_pds_docx_renderer.py`
- Modify: `tests/reporting/test_agent_workflow.py`

**Interfaces:**
- Produces: `PackagedDocxCore(template_path: Path).render_approved_prose(report_text, filename, report_model=None)`.
- Consumes: existing `PdsDocxRenderer` adapter contract.

- [ ] **Step 1: Add a test that removes the external handoff path**

```python
def test_packaged_core_renders_without_external_source(tmp_path: Path) -> None:
    template = tmp_path / "template.docx"
    Document().save(template)
    name, data = PackagedDocxCore(template).render_approved_prose("# 标题\n\n正文")
    assert name.endswith(".docx")
    assert "正文" in "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
```

- [ ] **Step 2: Run test and confirm RED because `PackagedDocxCore` does not exist**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/rendering/test_pds_docx_renderer.py -q`

- [ ] **Step 3: Implement the packaged renderer using the approved template**

```python
class PackagedDocxCore:
    def __init__(self, template_path: Path):
        self.template_path = Path(template_path)

    def render_approved_prose(self, report_text: str, *, filename=None, report_model=None):
        if report_model is not None:
            raise ValueError("structured-model prose generation is disabled")
        document = Document(self.template_path)
        append_markdown(document, report_text)
        buffer = io.BytesIO()
        document.save(buffer)
        return filename or "report.docx", buffer.getvalue()
```

- [ ] **Step 4: Make workflow use packaged core by default and remove absolute path**

```python
render = PdsDocxRenderer(
    PackagedDocxCore(template_path=self.service.report_template_path)
).render(report, output)
```

- [ ] **Step 5: Run renderer and complete workflow tests**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/rendering/test_pds_docx_renderer.py tests/reporting/test_agent_workflow.py -q`

### Task 3: Request gate and partial module execution

**Files:**
- Create: `autoreport/core/reporting/request_gate.py`
- Modify: `autoreport/core/reporting/models.py`
- Modify: `autoreport/core/reporting/service.py`
- Modify: `autoreport/core/reporting/workflow.py`
- Modify: `autoreport/core/tools/reporting_tool.py`
- Test: `tests/reporting/test_request_gate.py`
- Test: `tests/reporting/test_agent_workflow.py`
- Test: `tests/reporting/test_service_boundary.py`

**Interfaces:**
- Produces: `RequestGate.evaluate(request, coverage) -> GateDecision` with `proceed`, `status`, and `missing_evidence`.
- Produces: `ReportingRunResult.status` in `completed|blocked|failed`.

- [ ] **Step 1: Add failing gate tests for ask, block, draft, and skip**

```python
@pytest.mark.parametrize("policy,proceed,status", [
    ("ask", False, "blocked"),
    ("block", False, "blocked"),
    ("draft", True, "running"),
    ("skip", True, "running"),
])
def test_gate_honors_missing_evidence_policy(policy, proceed, status):
    decision = RequestGate.evaluate(request(policy), pending_coverage())
    assert decision.proceed is proceed
    assert decision.status == status
```

- [ ] **Step 2: Run gate tests and confirm RED**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/test_request_gate.py -q`

- [ ] **Step 3: Implement `GateDecision` and stop before Agent startup when blocked**

```python
class GateDecision(ReportingModel):
    proceed: bool
    status: Literal["running", "blocked"]
    missing_evidence: list[str]
```

- [ ] **Step 4: Add failing workflow test proving only requested modules run and requirements propagate**

```python
request = ReportRequest(
    instruction="重写设备模块",
    target_modules=["2.4"],
    execution_requirements=["深度核对现场图片"],
    missing_evidence_policy="draft",
)
await runner.run(state)
assert agents.specialist_ids == ["module-2.4-specialist"]
assert "深度核对现场图片" in agents.envelopes["module-2.4-specialist"].constraints
assert not (tmp_path / "Outputs/Reports/配电安全专家咨询报告.docx").exists()
```

- [ ] **Step 5: Honor target modules and branch full versus partial delivery**

Use `requested = tuple(state["request"].target_modules)` for planning and gather. Full five-module requests continue through cross review and DOCX; partial requests checkpoint and publish only module/review artifacts.

- [ ] **Step 6: Run request, workflow, and service tests**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/test_request_gate.py tests/reporting/test_agent_workflow.py tests/reporting/test_service_boundary.py tests/reporting/test_coverage.py -q`

### Task 4: Module Skill runtime injection

**Files:**
- Create: `autoreport/core/reporting/module_skills.py`
- Modify: `autoreport/core/reporting/prompts.py`
- Modify: `autoreport/core/reporting/agent_runner.py`
- Test: `tests/reporting/test_module_skills.py`
- Modify: `tests/reporting/test_prompts.py`

**Interfaces:**
- Produces: `ModuleSkillLibrary.packaged().for_agent(agent_id) -> list[ModuleSkill]`.
- Produces: `PromptAssembler.system_prompt(definition, module_skills=())`.

- [ ] **Step 1: Add tests for specialist and auditor Skill resolution**

```python
def test_specialist_receives_only_owned_module_skills() -> None:
    library = ModuleSkillLibrary.packaged()
    skills = library.for_agent("module-2.4-specialist")
    assert skills
    assert {skill.module_id for skill in skills} == {"2.4"}
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/test_module_skills.py -q`

- [ ] **Step 3: Implement deterministic loader and escaped XML assembly**

```python
def system_prompt(definition, module_skills=()):
    skill_xml = "\n".join(
        f'<module_skill id={quoteattr(skill.id)}>{escape(skill.content)}</module_skill>'
        for skill in module_skills
    )
```

Planner receives an index only; Specialist receives owned Skill contents; Auditor receives the module under review through task-scoped skill refs.

- [ ] **Step 4: Run prompt, runner, and config tests**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/test_module_skills.py tests/reporting/test_prompts.py tests/reporting/test_agent_runner.py tests/reporting/test_config.py -q`

### Task 5: Fixed submodule schema and protected local revisions

**Files:**
- Modify: `autoreport/core/reporting/agentic_models.py`
- Create: `autoreport/core/reporting/revision_guard.py`
- Modify: `autoreport/core/reporting/workflow.py`
- Modify: `autoreport/templates/reporting/agents/module-2.1-specialist.md`
- Modify: `autoreport/templates/reporting/agents/module-2.2-specialist.md`
- Modify: `autoreport/templates/reporting/agents/module-2.3-specialist.md`
- Modify: `autoreport/templates/reporting/agents/module-2.4-specialist.md`
- Modify: `autoreport/templates/reporting/agents/module-2.5-specialist.md`
- Modify: `autoreport/templates/reporting/agents/evidence-auditor.md`
- Test: `tests/reporting/test_agentic_models.py`
- Create: `tests/reporting/test_revision_guard.py`

**Interfaces:**
- Produces: `ModuleSubmission.submodule_narratives: dict[str, str]`.
- Produces: `RevisionGuard.validate(previous, revised, target_submodules) -> None`.

- [ ] **Step 1: Add failing taxonomy and preservation tests**

```python
def test_module_submission_requires_exact_fixed_submodules():
    with pytest.raises(ValueError, match="fixed submodules"):
        ModuleSubmission(module_id="2.4", submodule_narratives={"2.4.1": "x"}, ...)

def test_revision_guard_rejects_unrelated_submodule_drift():
    with pytest.raises(ValueError, match="2.4.2"):
        RevisionGuard.validate(before, changed_24_1_and_24_2, {"2.4.1"})
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/test_agentic_models.py tests/reporting/test_revision_guard.py -q`

- [ ] **Step 3: Add submodule fields and taxonomy validation**

```python
class ClaimRecord(StrictModel):
    submodule_id: str

class ModuleSubmission(StrictModel):
    submodule_narratives: dict[str, str]

    @model_validator(mode="after")
    def fixed_submodules(self):
        expected = set(REPORT_TAXONOMY[self.module_id].submodules)
        if set(self.submodule_narratives) != expected:
            raise ValueError("module submission requires exact fixed submodules")
        return self
```

- [ ] **Step 4: Route blocking issues by submodule and validate returned deltas**

Set `TaskEnvelope.target_submodule_ids`; require blocking module audit issues to name a submodule; call `RevisionGuard` before persisting a revision.

- [ ] **Step 5: Run model, revision, and workflow tests**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/test_agentic_models.py tests/reporting/test_revision_guard.py tests/reporting/test_agent_workflow.py -q`

### Task 6: Chief Editor protection plus photos and tables

**Files:**
- Modify: `autoreport/core/reporting/agentic_models.py`
- Create: `autoreport/core/reporting/assets.py`
- Modify: `autoreport/core/reporting/workflow.py`
- Modify: `autoreport/core/reporting/rendering/pds_docx_renderer.py`
- Create: `tests/reporting/test_assets.py`
- Modify: `tests/reporting/test_agent_workflow.py`
- Modify: `tests/reporting/rendering/test_pds_docx_renderer.py`

**Interfaces:**
- Produces: `ReportAssetAssembler.build(evidence, photos, claims, edited) -> tuple[list[ReportTable], list[ReportPhoto]]`.
- Produces: `validate_editor_protection(edited, claims) -> None`.

- [ ] **Step 1: Add failing protection and active asset tests**

```python
def test_editor_must_protect_and_preserve_every_approved_claim():
    with pytest.raises(ValueError, match="protected claim"):
        validate_editor_protection(edited_missing_claim, claims)

def test_active_workflow_passes_traceable_photo_to_renderer(tmp_path):
    await runner.run(state_with_photo_evidence)
    assert Document(report_path).inline_shapes
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/test_assets.py tests/reporting/test_agent_workflow.py -q`

- [ ] **Step 3: Add typed table/photo placement submissions and deterministic assembly**

```python
class TableSubmission(StrictModel):
    title: str
    headers: list[str]
    rows: list[list[str]]
    source_ids: list[str]
    claim_ids: list[str]

class EditedReportSubmission(StrictModel):
    tables: list[TableSubmission] = Field(default_factory=list)
    photo_ids: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Validate editor Claim set and exact anchors before ApprovedReport creation**

Require `set(edited.protected_claim_ids) == {claim.id for claim in claims}` and every resolved claim anchor to occur once in its module narrative.

- [ ] **Step 5: Run asset, renderer, and workflow tests**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/test_assets.py tests/reporting/rendering/test_pds_docx_renderer.py tests/reporting/test_agent_workflow.py -q`

### Task 7: Multi-format intake adapter registry

**Files:**
- Create: `autoreport/core/reporting/intake/adapters.py`
- Modify: `autoreport/core/reporting/models.py`
- Modify: `autoreport/core/reporting/service.py`
- Create: `tests/reporting/intake/test_adapters.py`
- Modify: `tests/reporting/intake/test_manifest.py`

**Interfaces:**
- Produces: `IntakeAdapterRegistry.parse(path, manifest_file) -> list[ParsedArtifact]`.
- Produces: explicit `manual_required` ParsedArtifact for DWG/video.

- [ ] **Step 1: Add failing tests for text, DOCX, image metadata, XLSX, and manual-required formats**

```python
@pytest.mark.parametrize("suffix", [".dwg", ".mp4"])
def test_unsupported_binary_format_is_explicit_manual_required(tmp_path, suffix):
    path = tmp_path / f"asset{suffix}"
    path.write_bytes(b"binary")
    artifact = registry.parse(path, manifest(path))[0]
    assert artifact.kind == "manual_required"
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/intake/test_adapters.py -q`

- [ ] **Step 3: Implement suffix-dispatched adapters and replace generic openpyxl loop**

```python
PARSERS = {
    ".xlsx": parse_workbook,
    ".docx": parse_docx,
    ".md": parse_text,
    ".txt": parse_text,
    ".png": parse_image,
    ".jpg": parse_image,
    ".jpeg": parse_image,
}
MANUAL_REQUIRED = {".dwg", ".mp4", ".mov", ".avi"}
```

PDF uses the available local PDF parser when installed; otherwise emits `manual_required` with reason `pdf_parser_unavailable`.

- [ ] **Step 4: Run intake, mapper, and service tests**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/intake tests/reporting/mappers tests/reporting/test_service_boundary.py -q`

### Task 8: YAML revision budgets and TaskBoard lifecycle

**Files:**
- Modify: `autoreport/core/reporting/workflow.py`
- Modify: `autoreport/core/reporting/service.py`
- Modify: `autoreport/core/reporting/agent_runner.py`
- Modify: `tests/reporting/test_agent_workflow.py`
- Modify: `tests/reporting/test_config.py`
- Modify: `tests/test_task_board.py`

**Interfaces:**
- Consumes: `WorkflowDefinition.phases[*].pipelines[*]`.
- Produces: TaskBoard records for each reporting Agent task.

- [ ] **Step 1: Add failing test with `maxRevisions: 1` and inspect task states**

```python
service.workflow = workflow_with_revision_budget(1)
with pytest.raises(AgentWorkflowError, match="revision budget"):
    await runner.run(state_requiring_two_revisions)
assert all(task.status in {"completed", "failed", "blocked"} for task in task_board.get_all())
```

- [ ] **Step 2: Run workflow tests and confirm RED from hard-coded `range(3)` / unused TaskBoard**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/test_agent_workflow.py tests/reporting/test_config.py -q`

- [ ] **Step 3: Resolve pipeline definitions by module and use declared budgets**

```python
pipeline = self.pipeline_by_module[module_id]
for revision in range(pipeline.max_revisions + 1):
    ...
```

- [ ] **Step 4: Wrap each `_agent()` call in TaskBoard create/start/complete/block/fail transitions**

Use stable descriptions `reporting:<workflow_id>:<agent_id>:<task_id>` and preserve successful module results when a sibling fails.

- [ ] **Step 5: Run workflow, config, manager, and TaskBoard tests**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/test_agent_workflow.py tests/reporting/test_config.py tests/test_manager.py tests/test_task_board.py -q`

### Task 9: Project-local Skill candidate governance

**Files:**
- Create: `autoreport/core/reporting/skills/__init__.py`
- Create: `autoreport/core/reporting/skills/governance.py`
- Create: `autoreport/core/reporting/skills/service.py`
- Create: `tests/reporting/skills/test_governance.py`

**Interfaces:**
- Produces: `SkillGovernanceStore.create_candidate()`, `.record_evaluation()`, `.publish()`, and `.rollback()`.
- Persists beneath `<project>/Capabilities/skills/`.

- [ ] **Step 1: Add failing lifecycle test**

```python
candidate = store.create_candidate(module_id="2.4", original="a", revised="b", reason="对象绑定错误")
evaluation = store.record_evaluation(candidate.id, baseline=0.5, candidate_score=0.9, regressions=[])
version = store.publish(candidate.id, evaluation.id, confirmed=True)
store.rollback(module_id="2.4", version_id=version.id)
assert store.active_version("2.4").id == version.id
```

- [ ] **Step 2: Run test and confirm RED**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/skills/test_governance.py -q`

- [ ] **Step 3: Implement strict Pydantic artifacts and atomic manifest writes**

Reject publish when `confirmed=False`, evaluation regresses, or candidate/evaluation module IDs differ. Never delete historical versions during rollback.

- [ ] **Step 4: Run governance and store tests**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run pytest tests/reporting/skills/test_governance.py tests/reporting/test_store.py -q`

### Task 10: Documentation and full verification

**Files:**
- Modify: `docs/superpowers/specs/2026-07-15-power-distribution-agent-system-design.md`
- Modify: `docs/superpowers/plans/2026-07-15-power-distribution-agentic-phase-a.md`
- Modify: `README.md`
- Modify: `README_zh.md`

**Interfaces:**
- Documents the same public contract verified by Tasks 1—9.

- [ ] **Step 1: Remove runtime 01/02 naming claims and document Knowledge/ plus import-only selection**

- [ ] **Step 2: Run source scans**

Run: `rg -n '01_页面导入知识库|02_本地skill提示词资料_禁止导入' autoreport/core autoreport/templates tests/reporting`

Expected: no runtime name-matching contract; historical design documents may mention the source folders only as import inputs.

- [ ] **Step 3: Run report subsystem**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache QT_QPA_PLATFORM=offscreen uv run pytest tests/reporting -q`

- [ ] **Step 4: Run deterministic full suite**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache QT_QPA_PLATFORM=offscreen uv run pytest -q -m 'not integration'`

- [ ] **Step 5: Run static and package checks**

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv run ruff check autoreport tests`

Run: `git diff --check`

Run: `UV_CACHE_DIR=/tmp/autoreport-uv-cache uv build`

- [ ] **Step 6: Audit requirements and Git state**

Confirm the current cleanup plus Tasks 1—9 are tracked, there is no absolute `/Users/.../work` production dependency, and no offline writer is reachable from `RunReportingWorkflowTool`.
