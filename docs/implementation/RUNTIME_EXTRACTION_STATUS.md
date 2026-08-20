# Runtime Extraction Implementation Status

> This is the single handoff record for the long-running Codex implementation program.
>
> Runtime execution state, provider traces, and product logs do not belong in this file.

## Program

- Repository: `csxq0605/manyselves`
- Plan base branch: `agent/declarative-runtime-plan`
- Implementation branch: `agent/declarative-runtime-implementation`
- Draft PR: <https://github.com/csxq0605/manyselves/pull/3>, targeting `agent/declarative-runtime-plan`
- Execution protocol: [`docs/CODEX_AUTONOMOUS_EXECUTION.md`](../CODEX_AUTONOMOUS_EXECUTION.md)

## Current position

- Current work package: `WP-06 in progress`
- Last completed work package: `WP-05`
- Current branch and latest implementation commit: `agent/declarative-runtime-implementation`; `WP-05: interpret declarative agent recovery policies` (the commit containing this status update)
- Current migration stage: `Stage 2 — Stateless Kernel and generic Runtime`
- Final real-test status: `deferred_until_all_four_stages_complete`
- Next automatic action: complete `R-01`/`R-02` focused research from current source and minimal POCs, then begin `WP-06` Characterization for neutral branching, looping, parallel Join, and subworkflow control flow

## Required startup checks

Codex must begin by running and recording:

```bash
git status -sb
git branch --show-current
git log -1 --oneline
```

The plan baseline must contain the autonomous execution commits and this status file. Codex must not implement on `main` or directly on `feature/react-fastapi-manyselves`.

## Completed commits

- `90fba9e` — `WP-00: stabilize plan baseline contracts`
- `9323b12` — `WP-00: freeze legacy semantic trace`
- `c7c1bd8` — `Program: defer real testing until four-stage completion`
- `396f2be` — `Program: remove residual gate resume wording`
- `5894cd5` — `WP-01: add definition models and file loaders`
- `e85dc03` — `WP-01: complete registry and contract adapters`
- `e1bfe9c` — `WP-02: execute neutral sequential workflows`
- `be100ae` — `WP-03: adapt current tools to declarative runtime`; current Tool binding, explicit contracts, unified outcome, existing result-index reuse, ReadTool coverage, and InvokeTool integration
- `71c1a9c` — `WP-04: bind neutral conversations to current agents`; neutral Conversation modes/keys/records, create/resolve actions, typed Agent port, InvokeAgent executor, and a Legacy ReportingAgentRunner adapter that passes the original session key
- `WP-05: interpret declarative agent recovery policies` — generic recovery events/actions/state, explicit Recovery Definition interpretation, Capability-owned correction prompts, declared-only attempt limits, and no-hash progress observations (this commit)
- `WP-05: align generic recovery event vocabulary` — aligned the Kernel enum exactly with plan section 5.8 and retained completed Tool Result reuse as the additional WP-05 event
- `WP-06 research: choose an internal control-flow runtime` — R-01/R-02 isolated POCs completed; MAF and LangGraph both classified as `reference`, with no production dependency change

## Tests actually run

- Before the user's focused-test-only instruction, the unmodified plan baseline full offline selection ran once: `2521 passed, 11 failed, 3 skipped, 6 deselected`. The 11 failures were existing test/config drift and platform/schema determinism issues, not runtime extraction changes.
- Each of those 11 baseline failures was rerun through focused/affected selections after correction: `92 passed`; the final OpenAPI/macOS-specific subset: `3 passed`.
- WP-00 semantic trace and Kernel boundary focused tests: `3 passed`.
- WP-00 affected Legacy recovery, conversation, review, and resume selection plus the new tests: `15 passed`.
- WP-01 models and single-file loader Characterization: initially failed collection because `manyselves.kernel.definitions` did not exist, then `8 passed` after implementation.
- WP-01 Registry and Contract Adapter Characterization: initially failed collection because the Registry and contracts package did not exist, then the complete WP-01 focused selection passed: `13 passed`.
- WP-01 affected Kernel import boundary, existing Reporting frontmatter/module-skill loaders, and Tool Registry selection: `30 passed`.
- WP-02 neutral sequential workflow Characterization: initially failed collection because the workflow/executor modules did not exist, then `7 passed` after implementation.
- WP-02 affected Kernel and Runtime selection: `22 passed`.
- WP-03 Tool Adapter Characterization: initially failed collection because the Tool port and adapter did not exist, then the Tool Adapter plus WP-02 integration selection passed: `13 passed`.
- WP-03 affected Runtime, Kernel workflow, existing Tool Result Index, Tool Outcome, Tool Registry, and selected ReadTool tests: `31 passed`. One initial command used an incorrect pytest class node and collected no tests; the corrected node selection is the recorded result.
- WP-04 Conversation and Agent Adapter Characterization: initially failed collection because the neutral Conversation package did not exist, then `7 passed`; a fresh-registry restoration case was added with the implementation.
- WP-04 affected Conversation, Agent Adapter, Kernel workflow, Tool Adapter, Kernel import boundary, and four existing Reporting identity/session selections: `26 passed`.
- WP-05 current-behavior Characterization for structured correction, Schema correction, Max Token continuation, Tool Slice continuation, productive slices, No-progress stop, and completed Tool Result reuse: `8 passed` before generic implementation.
- WP-05 generic Recovery Controller Characterization: initially failed collection because the Kernel recovery package did not exist, then `11 passed`.
- WP-05 affected generic recovery, Kernel import boundary, Legacy Agent adapter, existing reporting recovery/continuation, Schema correction, and Tool Result reuse selection: `24 passed`.
- WP-06 R-01 isolated POC: MAF 1.0.2 preserved two independent message histories (`[1, 3, 1]`) and produced 5 in-memory checkpoints; PowerFx condition execution was unavailable without .NET, and Goto to a ConditionGroup required its internal `_eval` ID.
- WP-06 R-02 isolated POC: LangGraph 1.2.11 completed a neutral two-conversation loop, dynamic three-branch parallel Join (`[2, 4, 6]`), output-contract validation, and 11 checkpoint snapshots.
- Ruff passed for every changed Python and test path. `git diff --check` passed.
- No real Provider was called. No full regression was rerun after the user's instruction.

## Research decisions

- R-01 Microsoft Agent Framework decision: `reference`.
- R-02 LangGraph decision: `reference`.
- WP-06 implementation choice: the existing lightweight internal Compiler/Executor, using current dependencies only.
- No new production orchestration dependency is approved or added.
- `jsonschema>=4.23,<5` was added only to execute the required generic JSON Schema Contract Adapter; it is not an orchestration dependency and is not used for runtime gates, security checks, hashes, or CAS.

The isolated POCs are recorded in `docs/research/DECLARATIVE_RUNTIME_LANDSCAPE.md`. The production path uses the current dependency set and a lightweight internal Compiler/Executor.

## Known blockers

- None. The real Legacy baseline is deferred into the single final real-test matrix. Automatic implementation continues through all four stages.

## Active user constraints

- Default to focused tests and affected test collections. Do not run a full regression suite unless the user explicitly requests it.
- Do not add unnecessary safety gates, decision gates, hashes, CAS, or extra validation chains. Preserve existing mechanisms without expanding them. If one becomes necessary, explain the concrete need, insufficiency of existing mechanisms, impact, alternatives, and rollback before implementation; explicit user approval is required.
- Do not create acceptance breakpoints or request intermediate real testing. Complete all four migration stages, the stateless Kernel, Definition layer, Reporting migration, generic projections, and second Capability before requesting one final real test.

## Four-stage continuous sequence

```text
Stage 1: WP-00..WP-01 — Baseline and Definition layer
Stage 2: WP-02..WP-06 — Stateless Kernel and generic Runtime
Stage 3: WP-07..WP-10 — Reporting migration and Capability package
Stage 4: WP-11..WP-12 — Generic API/UI projections and second Capability
Final: one real Provider/project/browser/server test handoff
```

## Resume instruction

A new Codex session should receive only:

```text
Read AGENTS.md and docs/implementation/RUNTIME_EXTRACTION_STATUS.md.
Confirm the current branch and latest commit.
Follow docs/CODEX_AUTONOMOUS_EXECUTION.md.
Continue from Current work package / Current migration stage without repeating completed work.
```
