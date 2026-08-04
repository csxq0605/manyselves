# Phase 1 Global Knowledge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a server-managed global knowledge library that every project can retrieve from while preserving project-knowledge priority and per-run provenance.

**Architecture:** `GlobalKnowledgeService` wraps the existing safe workspace-file layer at a fixed hidden data-root location. `ReferenceLibrary` accepts one optional global root and returns namespaced documents; `KnowledgeContextBuilder` snapshots selected sources into the existing run context without changing Agent or workflow contracts.

**Tech Stack:** Python 3.12, FastAPI, filesystem storage, existing ReferenceLibrary/KnowledgeContextBuilder/SourceLedger, React 19, TanStack Query.

**Current checkpoint (2026-08-04):** Task 1 is complete in `0d6f8b1`; Task 2 is complete in `554bdd5` (focused composite/research/source-ledger/knowledge-context 23 passed, targeted Ruff passed). Task 3 is active: freeze the composite source set per run, write namespaced SHA-256 provenance, and thread the optional fixed global root through runtime construction without changing Agent/workflow protocols.

## Global Constraints

- Store global knowledge at `<data_root>/.manyselves/global-knowledge/`.
- Do not copy it into projects and do not use symlinks.
- Project knowledge wins deterministic conflicts over global knowledge.
- Do not inject the entire global library into prompts; preserve bounded retrieval.
- Every used source must identify `project` or `global` and be reproducible for the run.
- Do not add Milvus, MinIO, MySQL, Redis, or LLM Wiki.

---

### Task 1: Global knowledge file service and API

**Files:**
- Create: `manyselves/application/global_knowledge_service.py`
- Create: `manyselves/webapi/schemas/global_knowledge.py`
- Create: `manyselves/webapi/routes/global_knowledge.py`
- Modify: `manyselves/webapi/dependencies.py`
- Modify: `manyselves/webapi/lifespan.py`
- Modify: `manyselves/webapi/main.py`
- Test: `tests/application/test_global_knowledge_service.py`
- Test: `tests/webapi/test_global_knowledge.py`
- Modify: `tests/webapi/test_openapi_contract.py`

**Interfaces:**
- Produces: `GlobalKnowledgeService(root: Path, *, max_text_bytes: int, max_upload_bytes: int, max_tree_entries: int)`.
- Produces: list/read/preview/upload/write/replace/delete/download operations accepting only relative paths.
- Produces: `/api/v1/global-knowledge/files` endpoints mirroring safe project-file DTOs without a project ID.

- [x] **Step 1: Write failing storage isolation and route tests**

```python
def test_service_root_is_hidden_global_directory(tmp_path: Path) -> None:
    service = GlobalKnowledgeService.from_data_root(
        tmp_path,
        max_text_bytes=2 * 1024 * 1024,
        max_upload_bytes=100 * 1024 * 1024,
        max_tree_entries=20_000,
    )
    assert service.root == (tmp_path / ".manyselves" / "global-knowledge").resolve()

@pytest.mark.asyncio
async def test_global_upload_never_creates_a_project(authed_client, tmp_path: Path) -> None:
    response = await authed_client.post(
        "/api/v1/global-knowledge/files/upload?path=standard.md&conflict=reject",
        files={"file": ("standard.md", b"rule", "text/markdown")},
    )
    assert response.status_code == 201
    assert response.json()["path"] == "standard.md"
    assert not (tmp_path / ".manyselves" / "Inputs").exists()
```

- [x] **Step 2: Run focused tests and verify failure**

Run: `uv run pytest tests/application/test_global_knowledge_service.py tests/webapi/test_global_knowledge.py -q`

Expected: FAIL because the service and router do not exist.

- [x] **Step 3: Implement service and API by composition**

Construct one `WorkspaceFiles` instance whose root is the fixed global directory. Reuse existing path, symlink, upload, revision, preview, and size protections. Do not accept an arbitrary root or absolute path from the request. Apply the session dependency at router level and the existing control lease to mutations. Return only relative logical paths and stable file error envelopes.

- [x] **Step 4: Run service, API, and OpenAPI tests**

Run: `uv run pytest tests/application/test_global_knowledge_service.py tests/webapi/test_global_knowledge.py tests/webapi/test_openapi_contract.py -q`

Expected: PASS.

- [x] **Step 5: Commit the global knowledge service**

```bash
git add manyselves/application/global_knowledge_service.py manyselves/webapi/schemas/global_knowledge.py manyselves/webapi/routes/global_knowledge.py manyselves/webapi/dependencies.py manyselves/webapi/lifespan.py manyselves/webapi/main.py tests/application/test_global_knowledge_service.py tests/webapi/test_global_knowledge.py tests/webapi/test_openapi_contract.py frontend-contract/openapi.json
git commit -m "feat: add global knowledge file service"
```

### Task 2: Composite ReferenceLibrary with deterministic project priority

**Files:**
- Modify: `manyselves/core/reporting/research/reference_library.py`
- Modify: `manyselves/core/reporting/research/__init__.py`
- Modify: `manyselves/core/tools/reporting_research_tools.py`
- Modify: `tests/reporting/research/test_reference_library.py`
- Modify: `tests/reporting/test_reporting_research_tools.py`

**Interfaces:**
- Produces: `KnowledgeNamespace = Literal["project", "global"]`.
- Produces: `ReferenceDocument.namespace` and `ReferenceHit.namespace`.
- Produces: `ReferenceLibrary(workspace: Path, *, global_root: Path | None = None)`.
- Produces: namespaced logical references `Knowledge/...` and `GlobalKnowledge/...` while physical paths stay hidden.

- [x] **Step 1: Write failing composite search tests**

```python
def test_project_document_wins_same_relative_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    global_root = tmp_path / "global"
    write(project / "Knowledge" / "rules.md", "project priority")
    write(global_root / "rules.md", "global priority")
    hits = ReferenceLibrary(project, global_root=global_root).search("priority", limit=10)
    assert [(hit.namespace, hit.relative_path) for hit in hits] == [
        ("project", "Knowledge/rules.md"),
    ]

def test_global_document_has_namespaced_reference(tmp_path: Path) -> None:
    document = ReferenceLibrary(project, global_root=global_root).open("GlobalKnowledge/shared.md")
    assert document.namespace == "global"
    assert document.relative_path == "GlobalKnowledge/shared.md"
```

- [x] **Step 2: Run research-library tests and verify failure**

Run: `uv run pytest tests/reporting/research/test_reference_library.py tests/reporting/test_reporting_research_tools.py -q`

Expected: FAIL because the library has one project-only root and no namespace.

- [x] **Step 3: Implement two-root traversal and safe opening**

Keep independent resolved roots and reject every candidate not beneath its selected root. Search project files first, then global files, de-duplicate by casefolded relative subpath and then by content SHA-256, and preserve project entries on conflict. Rank by score, then namespace priority (`project` before `global`), then logical path. Never return the physical global directory.

- [x] **Step 4: Update research tools to expose source namespace**

Add `namespace` to tool result records and ledger registration metadata without changing the existing tool names. Secret-safe serialization and output limits remain unchanged.

- [x] **Step 5: Run research tests**

Run: `uv run pytest tests/reporting/research/test_reference_library.py tests/reporting/test_reporting_research_tools.py -q`

Expected: PASS.

- [x] **Step 6: Commit composite retrieval**

```bash
git add manyselves/core/reporting/research manyselves/core/tools/reporting_research_tools.py tests/reporting/research/test_reference_library.py tests/reporting/test_reporting_research_tools.py
git commit -m "feat: compose project and global knowledge"
```

### Task 3: Run-stable knowledge context and provenance manifest

**Files:**
- Modify: `manyselves/core/reporting/research/knowledge_context.py`
- Modify: `manyselves/core/reporting/source_ledger.py`
- Modify: `manyselves/core/reporting/agent_runner.py`
- Modify: `manyselves/core/reporting/workflow.py`
- Modify: `manyselves/application/reporting_facade.py`
- Modify: `manyselves/application/runtime_host.py`
- Modify: `manyselves/webapi/lifespan.py`
- Modify: `tests/reporting/research/test_knowledge_context.py`
- Modify: `tests/reporting/test_agent_runner.py`
- Modify: `tests/reporting/test_agent_workflow.py`

**Interfaces:**
- Consumes: namespaced `ReferenceDocument` from Task 2.
- Produces: `KnowledgeContextBuilder(workspace: Path, run_id: str, *, global_root: Path | None = None)`.
- Produces: `Work/runs/<run-id>/context-manifests/knowledge-sources.json` containing logical path, namespace, and SHA-256 for selected documents.

- [x] **Step 1: Write failing provenance and mutation-stability tests**

```python
def test_context_manifest_records_project_and_global_sources(tmp_path: Path) -> None:
    context = KnowledgeContextBuilder(project, "run-1", global_root=global_root).build_module("2.1")
    manifest = json.loads((project / "Work/runs/run-1/context-manifests/knowledge-sources.json").read_text())
    assert {item["namespace"] for item in manifest["sources"]} == {"project", "global"}
    assert all(len(item["sha256"]) == 64 for item in manifest["sources"])

def test_started_builder_does_not_ingest_later_global_upload(tmp_path: Path) -> None:
    builder = KnowledgeContextBuilder(project, "run-1", global_root=global_root)
    builder.freeze_sources()
    write(global_root / "late.md", "late matching text")
    assert "late.md" not in builder.build_module("2.1").text
```

- [x] **Step 2: Run context/workflow tests and verify failure**

Run: `uv run pytest tests/reporting/research/test_knowledge_context.py tests/reporting/test_agent_runner.py tests/reporting/test_agent_workflow.py -q -k "knowledge or global"`

Expected: FAIL because source freezing and manifests do not exist.

- [x] **Step 3: Implement source freeze and manifest writing**

Freeze the composite document list at first use, before scoring. Write the manifest through `ReportingStore` beneath the current run. Register logical namespaced paths in `SourceLedger`; keep existing `R-*` IDs and deterministic budgets. Context wording must say “project/global knowledge reference” rather than mislabel global content as project-local.

- [x] **Step 4: Thread the configured global root through Runtime construction**

Pass `settings.data_root / ".manyselves" / "global-knowledge"` through `RuntimeHost`/`ReportingFacade` construction into every `ReferenceLibrary` and `KnowledgeContextBuilder` creation. Make the parameter optional so desktop and legacy tests without a configured server data root retain project-only behavior.

- [x] **Step 5: Run reporting and application regression tests**

Run: `uv run pytest tests/reporting/research tests/reporting/test_reporting_research_tools.py tests/reporting/test_agent_runner.py tests/reporting/test_agent_workflow.py tests/application/test_runtime_host.py -q`

Expected: PASS.

- [x] **Step 6: Commit run-stable global knowledge provenance**

```bash
git add manyselves/core/reporting manyselves/application/reporting_facade.py manyselves/application/runtime_host.py manyselves/webapi/lifespan.py tests/reporting tests/application/test_runtime_host.py
git commit -m "feat: snapshot global knowledge provenance per run"
```

### Task 4: Global knowledge React page

**Files:**
- Create: `frontend/src/features/knowledge/global-knowledge-api.ts`
- Create: `frontend/src/features/knowledge/GlobalKnowledgePage.tsx`
- Create: `frontend/src/features/knowledge/GlobalKnowledgePage.test.tsx`
- Create: `frontend/src/features/knowledge/knowledge.css`
- Modify: `frontend/src/app/routes.tsx`
- Modify: `frontend/src/features/shell/Sidebar.tsx`
- Modify: `frontend/e2e/workspace.spec.ts`

**Interfaces:**
- Consumes: global knowledge API from Task 1 and shared file components from Plan 08.
- Produces: `/knowledge` page with upload, text edit, preview, replace, delete, download, and indexing/source state.

- [ ] **Step 1: Write failing global knowledge page tests**

```tsx
it("uses browser file upload and never renders a server path picker", async () => {
  renderGlobalKnowledgePage();
  expect(screen.getByRole("button", { name: "上传文件" })).toBeVisible();
  expect(screen.queryByText("服务器文件")).not.toBeInTheDocument();
  expect(screen.queryByText(/\.manyselves/)).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `npm test -- --run src/features/knowledge`

Working directory: `frontend`

Expected: FAIL because the page does not exist.

- [ ] **Step 3: Implement the page using shared file capabilities**

Use the same FileList, preview, text editor, upload conflict, and delete confirmation components as project knowledge. The UI title is “全局知识库”; API paths remain logical and never render `.manyselves`. Display parse/index failure without deleting the uploaded file. Do not add vector-database controls.

- [ ] **Step 4: Regenerate frontend schema and run tests/E2E**

Run: `npm run generate:api`

Run: `npm run check:api`

Run: `npm test -- --run src/features/knowledge src/features/files`

Run: `npm run e2e -- workspace.spec.ts`

Working directory: `frontend`

Expected: PASS.

- [ ] **Step 5: Commit the global knowledge UI**

```bash
git add frontend/src/features/knowledge frontend/src/app/routes.tsx frontend/src/features/shell/Sidebar.tsx frontend/src/api/generated/schema.ts frontend/e2e/workspace.spec.ts
git commit -m "feat: add global knowledge workspace"
```
