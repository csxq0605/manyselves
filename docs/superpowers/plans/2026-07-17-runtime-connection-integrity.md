# Runtime Connection Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make artifact access, terminal results, workflow routing, completion, and provider usage executable and testable runtime contracts.

**Architecture:** Introduce a workflow-scoped Artifact Gateway and a normalized `ToolOutcome`, then route AgentLoop, reporting tools, collaboration messages, output verification, and usage accounting through them. Preserve existing public tool and TaskEnvelope names while replacing prompt-only assumptions with startup validation and real-loop tests.

**Tech Stack:** Python 3.12, asyncio, Pydantic 2, PyQt6, python-docx, openpyxl, Pillow, pytest/pytest-asyncio.

## Global Constraints

- General `read` must continue rejecting `.manyselves`.
- Default working memory remains 36,000 estimated tokens.
- Budgets may compact, checkpoint, replan, or block; they never decide completion.
- Only current-run verified artifacts may produce `completed`.
- No paid provider calls during implementation or verification.
- Preserve TaskEnvelope fields, reporting tool names, and existing project data.
- Follow red-green TDD and stage only task-scoped files in each commit.

---

### Task 1: Structured Tool Outcomes and Terminal Results

**Files:**
- Create: `manyselves/core/tools/outcomes.py`
- Modify: `manyselves/core/loops/agent_loop.py`
- Modify: `manyselves/core/tools/reporting_collaboration_tools.py`
- Test: `tests/core/tools/test_tool_outcomes.py`
- Test: `tests/core/loops/test_agent_loop_terminal.py`

**Interfaces:**
- Produces `ToolOutcome(status, terminal, result, error, artifact_refs)`.
- Produces `normalize_tool_outcome(value, tool_name) -> ToolOutcome`.
- Makes `submit_result` and `report_blocked` terminate the active AgentLoop.

- [ ] **Step 1: Write failing normalization tests**

```python
def test_failed_status_is_not_success():
    outcome = normalize_tool_outcome({"status": "failed", "error": "boom"})
    assert outcome.status == "failed"
    assert outcome.error == "boom"


def test_submit_result_is_terminal():
    outcome = normalize_tool_outcome(
        {"status": "completed", "result_path": "Work/runs/r/results/t.json"},
        tool_name="submit_result",
    )
    assert outcome.status == "ok"
    assert outcome.terminal is True
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/core/tools/test_tool_outcomes.py`

Expected: import failure because `outcomes.py` does not exist.

- [ ] **Step 3: Implement the minimal outcome model**

```python
class ToolOutcome(BaseModel):
    status: Literal["ok", "failed", "blocked"] = "ok"
    terminal: bool = False
    result: Any = None
    error: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
```

Normalize legacy dictionaries; map `failed/error` to failed, `blocked` to blocked, and mark the two terminal tools.

- [ ] **Step 4: Write a failing real-loop terminal test**

Use real `SubmitResultTool`, `MessageBus`, and `ToolRegistry` with a deterministic provider:

```python
await loop._handle_tool_calls(submit_response, "task-1")
assert provider.calls == 0
assert result_file.is_file()
```

- [ ] **Step 5: Verify RED**

Run: `.venv/bin/pytest -q tests/core/loops/test_agent_loop_terminal.py`

Expected: FAIL because the provider receives one post-submit call.

- [ ] **Step 6: Stop the loop after publishing terminal results**

In `_handle_tool_calls`, normalize every result, publish failed/blocked outcomes with `error`, append protocol-valid skipped results for later calls in the same batch, commit conversation history once, and return before the follow-up provider call.

- [ ] **Step 7: Verify GREEN**

Run: `.venv/bin/pytest -q tests/core/tools/test_tool_outcomes.py tests/core/loops/test_agent_loop_terminal.py tests/test_agent_loop.py tests/core/loops/test_agent_loop_retry.py`

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add manyselves/core/tools/outcomes.py manyselves/core/loops/agent_loop.py manyselves/core/tools/reporting_collaboration_tools.py tests/core/tools/test_tool_outcomes.py tests/core/loops/test_agent_loop_terminal.py
git commit -m "fix: make reporting results terminal and truthful"
```

### Task 2: Semantic Workflow Failure in Model Context and GUI

**Files:**
- Modify: `manyselves/core/tools/reporting_tool.py`
- Modify: `manyselves/core/loops/agent_loop.py`
- Modify: `manyselves/gui/main_window.py`
- Modify: `manyselves/gui/widgets/tool_call_group.py`
- Test: `tests/reporting/test_reporting_tool_outcomes.py`
- Test: `tests/gui/widgets/test_tool_call_group.py`

**Interfaces:**
- Consumes `ToolOutcome` from Task 1.
- Produces `reporting_result_outcome(payload) -> ToolOutcome`.
- Makes GUI success depend on semantic status, not only `error is None`.

- [ ] **Step 1: Write failing tests**

```python
def test_reporting_failure_is_failed_outcome():
    outcome = reporting_result_outcome({"status": "failed", "error": "render failed"})
    assert outcome.status == "failed"


def test_tool_group_marks_semantic_failure_failed(qtbot):
    group = ToolCallGroup()
    group.add_tool_call("run_reporting_workflow", {}, success=None)
    group.complete_tool_call(
        "run_reporting_workflow",
        result={"status": "failed", "error": "render failed"},
    )
    assert group.calls()[0].success is False


@pytest.mark.asyncio
async def test_reporting_failure_uses_canonical_terminal_response(loop, failed_tool):
    await loop._handle_tool_calls(failed_tool.response(), "message-1")
    assert loop.llm_provider.calls == 0
    assert "本次未交付" in canonical_responses(loop.bus)[0].content
```

- [ ] **Step 2: Verify RED**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/reporting/test_reporting_tool_outcomes.py tests/gui/widgets/test_tool_call_group.py`

Expected: semantic failure is currently treated as success.

- [ ] **Step 3: Normalize run, resume, and revise results**

Keep decision and scope-expansion payloads user-actionable, but publish failed/blocked states as non-success outcomes. Reporting entry-point outcomes are terminal and emit a deterministic user-facing status message from their structured payload, so Main cannot contradict the result in a follow-up generation. Add one GUI helper that derives an effective error from explicit error or structured status.

- [ ] **Step 4: Verify GREEN and commit**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/reporting/test_reporting_tool_outcomes.py tests/gui/widgets/test_tool_call_group.py tests/gui/test_main_window_task_format.py`

Expected: all selected tests pass.

```bash
git add manyselves/core/tools/reporting_tool.py manyselves/core/loops/agent_loop.py manyselves/gui/main_window.py manyselves/gui/widgets/tool_call_group.py tests/reporting/test_reporting_tool_outcomes.py tests/gui/widgets/test_tool_call_group.py
git commit -m "fix: surface semantic workflow failures"
```

### Task 3: Bounded Artifact Gateway and Correct Read Paging

**Files:**
- Create: `manyselves/core/artifacts/__init__.py`
- Create: `manyselves/core/artifacts/gateway.py`
- Create: `manyselves/core/tools/artifact_tools.py`
- Modify: `manyselves/core/loops/agent_loop.py`
- Modify: `manyselves/core/loops/manager.py`
- Modify: `manyselves/core/tools/file_tools.py`
- Test: `tests/core/artifacts/test_gateway.py`
- Test: `tests/test_file_tools.py`

**Interfaces:**
- Produces `ArtifactPage`, `ArtifactGrant`, `ArtifactGateway.open/search/open_internal`.
- Produces `OpenArtifactTool`, `OpenToolResultTool`, and `SearchTextTool`.
- Internal opaque references are bound to workflow, task, Agent, and session.

- [ ] **Step 1: Write failing paging and isolation tests**

```python
page = gateway.open("Work/long.txt", offset=0, limit=120)
assert page.total == 1000
assert page.truncated is True
assert page.next_offset == 120

ref = owner.persist_tool_result("call-1", "secret")
with pytest.raises(PermissionError):
    other_session.open_internal(ref, offset=0, limit=20)
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/core/artifacts/test_gateway.py`

Expected: gateway imports fail.

- [ ] **Step 3: Implement grants, signed opaque refs, paging, and search**

Use an HMAC-signed payload containing workflow, task, Agent, session, kind, and storage key. Enforce `1 <= limit <= 8000`, `1 <= max_matches <= 50`, and `0 <= context_lines <= 10`. Never accept a raw `.manyselves` path as authority. Route `AgentLoop._format_tool_result()` through the gateway so newly persisted large results immediately return usable opaque references.

- [ ] **Step 4: Write and run the `read(limit=...)` RED test**

```python
result = await ReadTool(tmp_path)(path="Work/long.txt", limit=3)
assert result["lines_read"] == 3
assert result["truncated"] is True
assert result["next_offset"] == 3
```

Run: `.venv/bin/pytest -q tests/test_file_tools.py::test_read_limit_without_offset_is_bounded`

Expected: FAIL because limit is ignored without offset.

- [ ] **Step 5: Implement bounded `ReadTool` slicing and register artifact tools**

Use `start = offset or 0`; apply `limit` independently; return total, returned count, truncation, and next offset. Register bounded artifact tools for the main Agent in `LoopManager`; reporting sessions receive scoped instances in Task 5.

- [ ] **Step 6: Verify GREEN and commit**

Run: `.venv/bin/pytest -q tests/core/artifacts/test_gateway.py tests/test_file_tools.py`

Expected: all selected tests pass.

```bash
git add manyselves/core/artifacts manyselves/core/tools/artifact_tools.py manyselves/core/tools/file_tools.py manyselves/core/loops/agent_loop.py manyselves/core/loops/manager.py tests/core/artifacts/test_gateway.py tests/test_file_tools.py
git commit -m "feat: add bounded artifact access gateway"
```

### Task 4: Truthful Format-Aware Inspection

**Files:**
- Create: `manyselves/core/artifacts/parsers.py`
- Modify: `manyselves/core/reporting/agent_runner.py`
- Modify: `manyselves/core/tools/pdf_tool.py`
- Test: `tests/core/artifacts/test_parsers.py`
- Test: `tests/reporting/test_agent_runner.py`

**Interfaces:**
- Produces `parse_artifact(path) -> ParsedArtifactBlocks`.
- Keeps `inspect_document` and `inspect_image` as compatibility adapters over the gateway.

- [ ] **Step 1: Write failing DOCX table, XLSX range, PDF, and image-state tests**

Assert DOCX tables appear as blocks, XLSX cells are readable, PDF/XLSX are never silently decoded as UTF-8, and metadata-only image inspection returns `visual_verified=False`.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/core/artifacts/test_parsers.py`

Expected: missing table/range blocks and silent binary behavior.

- [ ] **Step 3: Implement focused parsers**

```python
@dataclass(frozen=True)
class ArtifactBlock:
    locator: str
    text: str


@dataclass(frozen=True)
class ParsedArtifactBlocks:
    kind: Literal["text", "docx", "xlsx", "pdf", "image_metadata"]
    blocks: tuple[ArtifactBlock, ...]
    visual_verified: bool = False
```

Use `Document.tables`, `openpyxl.load_workbook(read_only=True, data_only=True)`, existing MinerU output when available, and explicit unsupported outcomes when it is unavailable.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/bin/pytest -q tests/core/artifacts/test_parsers.py tests/reporting/test_agent_runner.py tests/test_agent_tools.py`

Expected: all selected tests pass.

```bash
git add manyselves/core/artifacts/parsers.py manyselves/core/reporting/agent_runner.py manyselves/core/tools/pdf_tool.py tests/core/artifacts/test_parsers.py tests/reporting/test_agent_runner.py
git commit -m "feat: make artifact inspection format aware"
```

### Task 5: Compile Agent Capabilities and Enforce Allowed Outputs

**Files:**
- Create: `manyselves/core/reporting/capabilities.py`
- Modify: `manyselves/core/reporting/agent_runner.py`
- Modify: `manyselves/core/tools/reporting_collaboration_tools.py`
- Modify: `manyselves/templates/reporting/agents/report-planner.md`
- Modify: `manyselves/templates/reporting/agents/chief-editor.md`
- Test: `tests/reporting/test_capabilities.py`
- Test: `tests/reporting/test_collaboration_tools.py`

**Interfaces:**
- Produces `compile_agent_access(definition, envelope, refs) -> CompiledAgentAccess`.
- Extends `SubmitResultTool` with exact allowed submission discriminators.

- [ ] **Step 1: Write failing packaged-role capability tests**

```python
access = compile_agent_access(chief, envelope, envelope.input_refs)
assert access.unreadable_refs == ()
assert {d["name"] for d in registry.get_definitions()} >= {
    "open_artifact", "search_text", "submit_result"
}
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/reporting/test_capabilities.py`

Expected: Planner and Chief Editor lack concrete readers.

- [ ] **Step 3: Implement compilation and startup failure**

Compile declared carriers and attached refs into gateway grants. Fail before `loop.start()` with role, carrier, ref, and missing capability. Grant Planner and Chief Editor bounded artifact tools.

- [ ] **Step 4: Write the allowed-output RED test**

```python
with pytest.raises(SubmissionValidationError, match="allowed output"):
    await tool(payload=wrong_kind_payload)
assert not result_path.exists()
```

- [ ] **Step 5: Reject the wrong discriminator before persistence**

Map `module_submission`, `plan_submission`, `audit_submission`, `cross_review_submission`, `workflow_decision_submission`, `edited_report_submission`, and `skill_evolution_submission` to TaskEnvelope allowed outputs.

- [ ] **Step 6: Verify GREEN and commit**

Run: `.venv/bin/pytest -q tests/reporting/test_capabilities.py tests/reporting/test_collaboration_tools.py tests/reporting/test_config.py tests/reporting/test_prompts.py`

Expected: all selected tests pass.

```bash
git add manyselves/core/reporting/capabilities.py manyselves/core/reporting/agent_runner.py manyselves/core/tools/reporting_collaboration_tools.py manyselves/templates/reporting/agents/report-planner.md manyselves/templates/reporting/agents/chief-editor.md tests/reporting/test_capabilities.py tests/reporting/test_collaboration_tools.py
git commit -m "fix: enforce reporting agent capabilities"
```

### Task 6: Workflow-Scoped Router and Message Consumers

**Files:**
- Create: `manyselves/core/reporting/message_router.py`
- Modify: `manyselves/core/reporting/agent_runner.py`
- Modify: `manyselves/core/reporting/workflow.py`
- Modify: `manyselves/core/tools/reporting_collaboration_tools.py`
- Modify: `manyselves/core/tools/reporting_research_tools.py`
- Test: `tests/reporting/test_message_router.py`
- Test: `tests/reporting/test_reporting_research_tools.py`

**Interfaces:**
- Produces `WorkflowMessageRouter.register_session/unregister_session/close`.
- Produces run-scoped collections for research notes, revision requests, gaps, and blocked notices.

- [ ] **Step 1: Write failing stale-router and parallel-session tests**

Close runner A, start runner B, query a live peer, and assert A cannot answer. Register two auditor sessions and assert a session-qualified query reaches only its target.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/reporting/test_message_router.py::test_closed_router_cannot_answer_later_workflow tests/reporting/test_message_router.py::test_parallel_same_identity_sessions_are_distinct`

Expected: stale unavailable reply wins and identity-only routing is ambiguous.

- [ ] **Step 3: Implement one workflow-owned router and cleanup**

Key sessions by `(workflow_id, agent_id, session_id)`. Extend `query_peer` with optional `target_session_id`; reject ambiguous same-identity routing when it is omitted. Subscribe once per workflow, filter every message, and unsubscribe all callbacks in `close_workflow()`.

- [ ] **Step 4: Write failing consumer tests**

Publish `RevisionRequestMessage`, `ResearchNotePublishedMessage`, `ProgressNoteMessage`, and `BlockedNoticeMessage`; assert the corresponding workflow collection changes and the next responsible TaskEnvelope receives only the reference.

- [ ] **Step 5: Implement consumers and acknowledgements**

Persist references, not long content. Revision requests become issue refs; research notes enter a run index; gaps and blocks update workflow state and user notices.

- [ ] **Step 6: Verify GREEN and commit**

Run: `.venv/bin/pytest -q tests/reporting/test_message_router.py tests/reporting/test_agent_runner.py tests/reporting/test_reporting_research_tools.py tests/reporting/test_collaboration_tools.py`

Expected: all selected tests pass.

```bash
git add manyselves/core/reporting/message_router.py manyselves/core/reporting/agent_runner.py manyselves/core/reporting/workflow.py manyselves/core/tools/reporting_collaboration_tools.py manyselves/core/tools/reporting_research_tools.py tests/reporting/test_message_router.py tests/reporting/test_reporting_research_tools.py
git commit -m "fix: scope reporting messages to workflows"
```

### Task 7: Preserve Blocked State and Verify Every Completion

**Files:**
- Create: `manyselves/core/reporting/output_verifier.py`
- Modify: `manyselves/core/reporting/workflow.py`
- Modify: `manyselves/core/reporting/service.py`
- Modify: `manyselves/core/reporting/revisions.py`
- Test: `tests/reporting/test_output_verifier.py`
- Test: `tests/reporting/test_service_boundary.py`
- Test: `tests/reporting/test_revision_coordinator.py`

**Interfaces:**
- Produces `verify_current_run_outputs(workspace, run_id, artifacts, started_ns) -> list[Path]`.
- Produces typed `AgentWorkflowBlocked` instead of collapsing blocked into failed.

- [ ] **Step 1: Write and run the blocked-state RED test**

Run: `.venv/bin/pytest -q tests/reporting/test_service_boundary.py::test_agent_blocked_state_is_preserved`

Expected: current result is `failed`, not blocked.

- [ ] **Step 2: Add typed blocked propagation**

Carry role, task, reason, and artifact refs to Service; map to blocked or durable user decision according to missing-evidence policy.

- [ ] **Step 3: Write failing shared-verifier tests**

Cover empty, missing, stale, outside-workspace, invalid DOCX, wrong-run receipt, and valid current-run artifacts. Assert initial, resume, and revision use the same verifier.

- [ ] **Step 4: Implement and wire the verifier**

Revision captures `execution_started_ns` before work. None of the three entry points may construct `completed` without verified paths and matching delivery receipt.

- [ ] **Step 5: Verify GREEN and commit**

Run: `.venv/bin/pytest -q tests/reporting/test_output_verifier.py tests/reporting/test_service_boundary.py tests/reporting/test_revision_coordinator.py tests/reporting/test_agent_workflow.py`

Expected: all selected tests pass.

```bash
git add manyselves/core/reporting/output_verifier.py manyselves/core/reporting/workflow.py manyselves/core/reporting/service.py manyselves/core/reporting/revisions.py tests/reporting/test_output_verifier.py tests/reporting/test_service_boundary.py tests/reporting/test_revision_coordinator.py
git commit -m "fix: verify every reporting completion"
```

### Task 8: Record Every Provider Attempt and Guard Call

**Files:**
- Create: `manyselves/core/usage_ledger.py`
- Modify: `manyselves/core/loops/agent_loop.py`
- Modify: `manyselves/core/reporting/agent_runner.py`
- Test: `tests/core/test_usage_ledger.py`
- Test: `tests/core/loops/test_agent_loop_retry.py`
- Test: `tests/test_agent_loop.py`

**Interfaces:**
- Produces `UsageLedger.record_attempt(...)` with a run lock and atomic JSONL append.
- Replaces logical-request accounting with per-attempt rows.

- [ ] **Step 1: Write and run the retry-ledger RED test**

```python
await loop._chat_with_retries(messages, None, "m1", phase="initial")
rows = load_rows(run_id)
assert [row["attempt"] for row in rows] == [1, 2, 3]
assert [row["status"] for row in rows] == ["error", "error", "success"]
```

Run: `.venv/bin/pytest -q tests/core/loops/test_agent_loop_retry.py::test_every_provider_attempt_is_recorded`

Expected: attempts are collapsed or absent.

- [ ] **Step 2: Implement the locked ledger inside the retry loop**

Record provider or estimated source, phase, round, attempt, retry, guard, classified error, and task/Agent/run cumulative totals. UI debug messages aggregate rows without writing a duplicate row.

- [ ] **Step 3: Write guard and concurrent-writer RED tests**

Assert both report guards write `guard=True`; concurrently append from five loops and parse every line; assert run totals equal row sums.

- [ ] **Step 4: Instrument guards and verify GREEN**

Run: `.venv/bin/pytest -q tests/core/test_usage_ledger.py tests/core/loops/test_agent_loop_retry.py tests/test_agent_loop.py`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add manyselves/core/usage_ledger.py manyselves/core/loops/agent_loop.py manyselves/core/reporting/agent_runner.py tests/core/test_usage_ledger.py tests/core/loops/test_agent_loop_retry.py tests/test_agent_loop.py
git commit -m "fix: account for every provider attempt"
```

### Task 9: Recoverable, Non-duplicated Working-Memory Checkpoints

**Files:**
- Modify: `manyselves/core/loops/agent_loop.py`
- Modify: `manyselves/core/artifacts/gateway.py`
- Test: `tests/test_context_compaction.py`
- Test: `tests/core/artifacts/test_gateway.py`

**Interfaces:**
- Consumes opaque internal refs from Task 3.
- Produces checkpoint messages containing `checkpoint_ref`.

- [ ] **Step 1: Write failing recovery and deduplication tests**

Assert the owner can page removed transcript content, another session is rejected, and two no-tool turns do not persist the same removed transcript twice.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q tests/test_context_compaction.py::test_checkpoint_is_recoverable tests/test_context_compaction.py::test_no_tool_compaction_is_not_repeated`

Expected: checkpoint has no usable ref and duplicate files appear.

- [ ] **Step 3: Persist once and replace active history**

Include opaque `checkpoint_ref` in the compact message. After a successful request, replace `_conversation_history` with compacted non-system messages at a legal provider boundary.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/bin/pytest -q tests/test_context_compaction.py tests/core/artifacts/test_gateway.py tests/test_agent_loop.py`

Expected: all selected tests pass.

```bash
git add manyselves/core/loops/agent_loop.py manyselves/core/artifacts/gateway.py tests/test_context_compaction.py tests/core/artifacts/test_gateway.py
git commit -m "fix: make compacted context recoverable"
```

### Task 10: Real Runtime Contract E2E and Final Verification

**Files:**
- Create: `tests/reporting/test_runtime_contract_e2e.py`
- Modify: `tests/reporting/test_agent_workflow.py`
- Modify: `docs/capabilities/power-distribution.md`

**Interfaces:**
- Consumes Tasks 1-9.
- Produces a deterministic fake-provider scenario using real AgentLoop and MessageBus.

- [ ] **Step 1: Write the real-loop E2E test**

The fake provider must refuse submission until it has opened every required attached artifact. Assert terminal provider calls are zero, router subscriptions are gone, usage rows cover calls, and the output receipt matches the current run.

- [ ] **Step 2: Verify RED for remaining integration gaps**

Run: `QT_QPA_PLATFORM=offscreen PYTHONPYCACHEPREFIX=/tmp/manyselves-pycache .venv/bin/pytest -q tests/reporting/test_runtime_contract_e2e.py`

Expected: fail only on a remaining production connection, not fixture setup.

- [ ] **Step 3: Fix each exposed connection with its own red-green cycle**

Do not bypass AgentLoop or weaken the assertions. Add each new regression to the smallest owning test file.

- [ ] **Step 4: Update capability documentation**

Document bounded search/paging, parser truthfulness, structured statuses, terminal submission, and usage ledger semantics. State that no paid-provider E2E was run.

- [ ] **Step 5: Run targeted verification**

```bash
QT_QPA_PLATFORM=offscreen PYTHONPYCACHEPREFIX=/tmp/manyselves-pycache .venv/bin/pytest -q tests/core/artifacts tests/core/tools/test_tool_outcomes.py tests/core/loops/test_agent_loop_terminal.py tests/core/loops/test_agent_loop_retry.py tests/core/test_usage_ledger.py tests/reporting/test_capabilities.py tests/reporting/test_message_router.py tests/reporting/test_output_verifier.py tests/reporting/test_runtime_contract_e2e.py tests/reporting/test_agent_runner.py tests/reporting/test_agent_workflow.py tests/reporting/test_service_boundary.py tests/reporting/test_revision_coordinator.py tests/gui/widgets/test_tool_call_group.py
```

Expected: all selected tests pass without unconsumed-message or pending-task warnings.

- [ ] **Step 6: Run complete fresh verification**

```bash
QT_QPA_PLATFORM=offscreen PYTHONPYCACHEPREFIX=/tmp/manyselves-pycache .venv/bin/pytest -q -m 'not integration'
PYTHONPYCACHEPREFIX=/tmp/manyselves-pycache .venv/bin/python -m compileall -q manyselves
git diff --check
```

Expected: zero failures, no unexpected skips, compile exit 0, and empty diff-check output.

- [ ] **Step 7: Audit runtime declarations**

Run repository checks proving every packaged role passes the capability compiler and every production workflow message in the design has exactly one workflow-scoped consumer. Include the outputs in the handoff.

- [ ] **Step 8: Commit**

```bash
git add tests/reporting/test_runtime_contract_e2e.py tests/reporting/test_agent_workflow.py docs/capabilities/power-distribution.md
git commit -m "test: verify reporting runtime connections end to end"
```
