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

- Current work package: `WP-02 in progress`
- Last completed work package: `WP-01`
- Current branch and latest implementation commit: `agent/declarative-runtime-implementation`; `WP-01: complete registry and contract adapters` (the commit containing this status update)
- Current migration stage: `Stage 2 — Stateless Kernel and generic Runtime`
- Final real-test status: `deferred_until_all_four_stages_complete`
- Next automatic action: begin `WP-02` Characterization for WorkflowState, ResolvedAction/ResolvedPlan, minimal sequential Compiler, Executor Registry, and File State Store

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
- `WP-01: complete registry and contract adapters` — capability-directory loading, typed reference resolution, Pydantic/JSON Schema adapters, and WP-01 affected verification (this commit)

## Tests actually run

- Before the user's focused-test-only instruction, the unmodified plan baseline full offline selection ran once: `2521 passed, 11 failed, 3 skipped, 6 deselected`. The 11 failures were existing test/config drift and platform/schema determinism issues, not runtime extraction changes.
- Each of those 11 baseline failures was rerun through focused/affected selections after correction: `92 passed`; the final OpenAPI/macOS-specific subset: `3 passed`.
- WP-00 semantic trace and Kernel boundary focused tests: `3 passed`.
- WP-00 affected Legacy recovery, conversation, review, and resume selection plus the new tests: `15 passed`.
- WP-01 models and single-file loader Characterization: initially failed collection because `manyselves.kernel.definitions` did not exist, then `8 passed` after implementation.
- WP-01 Registry and Contract Adapter Characterization: initially failed collection because the Registry and contracts package did not exist, then the complete WP-01 focused selection passed: `13 passed`.
- WP-01 affected Kernel import boundary, existing Reporting frontmatter/module-skill loaders, and Tool Registry selection: `30 passed`.
- Ruff passed for every changed Python and test path. `git diff --check` passed.
- No real Provider was called. No full regression was rerun after the user's instruction.

## Open research decisions

- Whether to adopt, adapt, or only reference Microsoft Agent Framework Declarative Workflow remains undecided.
- Whether LangGraph or an internal lightweight compiler/runtime should be used remains undecided.
- No new production orchestration dependency is approved.
- `jsonschema>=4.23,<5` was added only to execute the required generic JSON Schema Contract Adapter; it is not an orchestration dependency and is not used for runtime gates, security checks, hashes, or CAS.

These questions do not block the completed `WP-00` or implementation of `WP-01`. This implementation run will not add a production orchestration dependency; research and POCs remain isolated while the production path uses the current dependency set and a lightweight internal Compiler/Executor.

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
