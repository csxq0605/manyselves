# Manyselves Runtime Connection Integrity Design

Date: 2026-07-17

## Problem

Manyselves currently persists references and publishes typed messages that are not always
consumable by the Agent or workflow that receives them. Several declarative contracts describe
capabilities that the runtime does not enforce. These gaps can cause an Agent to assume it read
an artifact that it could not access, continue spending tokens after terminal submission, display
a semantic workflow failure as a successful tool call, or lose workflow-scoped collaboration
messages.

This design makes access, terminal state, routing, completion, and usage accounting executable
runtime contracts rather than prompt conventions.

## Goals

- Every artifact reference given to an Agent is accessible through an explicitly granted tool.
- Large content remains outside model context but is searchable and pageable on demand.
- Terminal reporting tools stop their AgentLoop immediately.
- Business failure, blocked state, and successful tool execution remain distinct end to end.
- Workflow messages have real consumers and cannot leak across workflow or session boundaries.
- Completion requires verified artifacts from the current run for initial, resumed, and revision
  workflows.
- Usage accounting records every provider attempt, including retries and guard rounds.
- The working-memory target remains approximately 36k tokens and never decides completion.

## Non-goals

- General access to the entire `.manyselves` metadata tree.
- Replacing professional review decisions with fixed revision or token budgets.
- Reinjecting full persisted transcripts into model context automatically.
- Redesigning the report taxonomy, visual interface, or provider abstraction beyond the runtime
  connections needed here.
- Running a paid live-provider validation without separate user approval.

## Chosen Approach

Use a unified, workflow-scoped Artifact Gateway and a structured tool outcome protocol.

This is preferred over adding unrelated offset parameters to individual tools because a shared
gateway can enforce the same path, session, pagination, and truncation rules everywhere. It is
preferred over reinjecting full content because model context remains bounded and usage stays
observable.

## Artifact Gateway

### Public tools

The gateway exposes three bounded tools:

- `search_text(query, refs, globs, max_matches, context_lines)` searches only authorized project
  artifacts and returns bounded matching passages with stable locators.
- `open_artifact(ref, offset, limit)` reads an authorized project artifact through the appropriate
  parser and returns one page.
- `open_tool_result(ref, offset, limit)` reads only the current Agent's persisted tool result or
  context checkpoint through an opaque, workflow-scoped reference.

Every page returns:

- `ref`
- `content`
- `unit` (`chars`, `lines`, `rows`, `pages`, or `blocks`)
- `offset`
- `returned`
- `total`
- `truncated`
- `next_offset`, when more content exists

The default and maximum page sizes are enforced in code. A caller cannot request an unbounded
page.

### Parser behavior

- UTF-8 text, Markdown, JSON, JSONL, CSV, and HTML support line or character paging.
- DOCX exposes paragraphs and table rows as stable blocks.
- XLSX exposes workbook metadata, sheet ranges, and cell values without binary text decoding.
- PDF exposes extracted page text and page metadata through the existing PDF parsing boundary.
- Images expose actual visual inspection through the configured image-capable path when
  available. Metadata-only inspection is named and reported as metadata-only; it never claims
  visual verification.

Unsupported or unavailable parsing produces an explicit failed outcome with the unsupported
region or format. Silent UTF-8 decoding of binary formats is forbidden.

### Internal access

General `read` continues to reject `.manyselves`. The gateway resolves opaque references against
an allowlist containing only:

- the current workflow and run,
- the current task and Agent session,
- the specific persisted tool result or checkpoint,
- project artifacts explicitly attached to the TaskEnvelope.

Raw internal paths are not accepted as authority. Cross-run or cross-session references fail.

### Capability compilation

Before starting an Agent task, the runtime compiles an access manifest from:

- the Agent definition's `reads` and tools,
- TaskEnvelope input, shared, prior-result, issue, and context-summary references,
- workflow, run, task, and session identity.

Startup fails before the provider is called when a required carrier has no concrete access path.
The compiled manifest grants tools; frontmatter alone does not grant access. Planner and Chief
Editor receive the artifact tools required for their declared inputs.

`allowed_outputs` is compiled into `submit_result` and enforced before persistence. A payload of a
different submission kind is a validation failure, not a completed result followed by a workflow
type error.

## Structured Tool Outcomes

All tools are normalized to one runtime envelope:

```text
status: ok | failed | blocked
terminal: true | false
result: structured value or null
error: structured error or null
artifact_refs: zero or more references
```

- `ok, terminal=false` continues the tool loop.
- `failed, terminal=false` is published as an error to the GUI and model context. It may be
  retried only by the existing bounded retry and progress policies.
- `blocked, terminal=true` publishes the blocked result and stops the AgentLoop.
- `ok, terminal=true` publishes the typed result and stops the AgentLoop.

`submit_result` and `report_blocked` are terminal. After their result is published, remaining tool
calls in the same batch are returned as skipped terminal calls, no follow-up provider request is
made, and the final conversation state is committed once.

Reporting workflow tools translate `ReportingRunResult.status` into the same envelope. A
workflow result of `failed`, `blocked`, `incomplete`, `needs_decision`, or
`needs_scope_expansion` is never displayed as a successful completion. Non-error decision states
remain structured and user-actionable.

## Workflow Status and Completion

Agent result states remain distinct:

- `completed` carries an allowed typed payload.
- `blocked` carries a concrete missing input or required decision and propagates through the
  workflow without conversion to a generic failure.
- `incomplete` means the Agent ended without a terminal typed result.
- `failed` means execution or contract failure.

A shared output verifier is used by initial runs, resumed runs, and post-delivery revisions. It
requires:

- at least one expected output artifact,
- every path to remain inside the workspace,
- every file to exist and be a regular file,
- modification during the current execution or an immutable version snapshot created by it,
- format-level openability for DOCX and other packaged outputs,
- a delivery receipt tied to the current run.

Only the verifier can produce `completed`. Main's user-facing response consumes this structured
result; prompt wording is a secondary guard, not the source of truth.

## Workflow Message Routing

One workflow-scoped router owns all collaboration subscriptions. Its registry key is
`(workflow_id, agent_id, session_id)`, not only `agent_id`.

- `PeerQueryMessage` routes only to a live matching workflow session. An unavailable response is
  emitted only by the active workflow router after checking its own registry.
- `RevisionRequestMessage` is consumed by the revision coordinator, persisted, and attached to
  the responsible Agent's next TaskEnvelope. It has an acknowledgement or terminal failure.
- `ResearchNotePublishedMessage` updates a run-scoped research index and makes the note reference
  available to relevant active tasks without injecting its full content.
- `ProgressNoteMessage` updates workflow state and user-visible progress when appropriate.
- `BlockedNoticeMessage` updates blocked state and decision requirements.
- `AgentResultMessage` remains the authoritative terminal Agent message.

Closing a workflow stops sessions, removes registry entries, and unsubscribes every callback.
Runner instances cannot reply to later workflows. Parallel sessions of the same identity remain
distinct.

## Context and File Reading

General `read(path, offset, limit)` applies `limit` even when offset is omitted. It returns total
line count, lines read, truncation, and next offset.

Tool-result compaction persists full serialized results and returns an opaque reference plus a
bounded preview. Working-memory compaction persists removed transcript blocks and includes the
opaque checkpoint reference in the checkpoint message. Both are recoverable through the gateway.

When a non-tool conversation is compacted, the compacted history replaces the in-memory history
at a legal provider boundary. This avoids persisting the same removed transcript repeatedly.

No context or token budget marks a task complete. Limits may trigger compaction, checkpointing,
replanning, or an explicit blocked/incomplete result.

## Usage Accounting

The ledger records one row per provider attempt, not one row per logical round. Required fields
include:

- run, workflow, task, Agent, session,
- phase, round, and attempt,
- model,
- status and classified error,
- provider or estimated usage source,
- input, output, and total tokens,
- retry and guard indicators,
- cumulative task, Agent, and run totals.

Initial calls, tool follow-ups, automatic retries, partial failures, report guards, and blocked
guards all use the same recorder. Concurrent writers use a run-scoped lock and atomic line append.

Anomaly thresholds can emit a progress notice, force compaction, and request replanning. They do
not stop a task or declare completion.

## Compatibility and Migration

- Existing TaskEnvelope fields and public reporting tool names remain valid.
- Existing project data does not require rebuilding.
- Existing `.manyselves/tool-results` content can be opened through the new restricted tool after
  an opaque reference is issued for the current session.
- Existing Agent definitions are validated at startup. Packaged definitions are updated where
  their declared carriers currently lack tools.
- Legacy raw internal references are not accepted directly by the gateway.
- The current 36k working-memory target remains the default.

## Test Strategy

Development follows red-green TDD. The required tests cover:

1. Large tool results can be searched, paged, and reconstructed exactly.
2. Internal references cannot cross workflow, task, Agent, or session boundaries.
3. Planner and Chief Editor can open every attached artifact they are expected to consume.
4. PDF, DOCX tables, XLSX ranges, and image metadata/visual states are explicit and truthful.
5. `submit_result` and `report_blocked` cause zero subsequent provider calls.
6. Semantic failed and blocked outcomes are errors or blocked states in model context, GUI, and
   persisted run state.
7. Closed routers cannot observe or answer later workflow messages.
8. Revision requests, research notes, gaps, and blocked notices have real consumers.
9. Every provider retry and guard round produces a separate ledger row.
10. Initial, resumed, and revision workflows use the same output verifier.
11. Working-memory compaction can recover required persisted content without exceeding its
    target.
12. `read(limit=...)` is bounded without requiring an explicit offset.
13. `allowed_outputs` rejects a mismatched submission before persisting a completed result.
14. Packaged Agent definitions pass the compiled capability matrix.

Critical workflow tests use a real AgentLoop and MessageBus with a deterministic local fake
provider. Scripted payload tests remain useful for pure orchestration, but they cannot serve as
evidence that runtime connections work.

## Verification and Acceptance

Completion requires all of the following fresh evidence:

- each regression test was observed failing before its implementation and passing afterward,
- targeted AgentLoop, artifact, routing, reporting, revision, GUI tool-status, and usage tests
  pass,
- the complete non-integration test suite passes with no unexpected skips,
- Python compilation and `git diff --check` pass,
- a controlled local fake-provider end-to-end report exercises reading, submission, routing,
  output verification, persistence, and shutdown,
- no packaged Agent declares a carrier it cannot consume,
- no production workflow message type used here lacks a consumer,
- no paid provider call is made without separate user approval.
