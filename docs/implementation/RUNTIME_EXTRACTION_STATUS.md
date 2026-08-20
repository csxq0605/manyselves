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

- Current work package: `WP-00 complete`
- Last completed work package: `WP-00`
- Current branch and latest implementation commit: `agent/declarative-runtime-implementation`; `WP-00: freeze legacy semantic trace` (the commit containing this status update)
- Current human gate: `HG-00`
- Human gate result: `awaiting_human_execution`
- Next automatic action: wait for `APPROVE_GATE HG-00`; record the supplied real Legacy Run ID and artifacts, then begin `WP-01` without repeating WP-00

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
- `WP-00: freeze legacy semantic trace` — stable semantic events, Legacy module-lane characterization, Kernel import boundary, and WP-00 baseline document (this commit)

## Tests actually run

- Before the user's focused-test-only instruction, the unmodified plan baseline full offline selection ran once: `2521 passed, 11 failed, 3 skipped, 6 deselected`. The 11 failures were existing test/config drift and platform/schema determinism issues, not runtime extraction changes.
- Each of those 11 baseline failures was rerun through focused/affected selections after correction: `92 passed`; the final OpenAPI/macOS-specific subset: `3 passed`.
- WP-00 semantic trace and Kernel boundary focused tests: `3 passed`.
- WP-00 affected Legacy recovery, conversation, review, and resume selection plus the new tests: `15 passed`.
- Ruff passed for every changed Python and test path. `git diff --check` passed.
- No real Provider was called. No full regression was rerun after the user's instruction.

## Open research decisions

- Whether to adopt, adapt, or only reference Microsoft Agent Framework Declarative Workflow remains undecided.
- Whether LangGraph or an internal lightweight compiler/runtime should be used remains undecided.
- No new production orchestration dependency is approved.

These questions do not block the completed `WP-00`. They must only trigger `HG-01` if a later POC shows that a production dependency or long-term public interface decision is necessary.

## Known blockers

- Automatic work is paused only for the required `HG-00` real Legacy baseline. The test instructions and evidence checklist are in [`WP_00_LEGACY_BASELINE.md`](WP_00_LEGACY_BASELINE.md).

## Active user constraints

- Default to focused tests and affected test collections. Do not run a full regression suite unless the user explicitly requests it, or a special human-gate instruction requires it after Codex states the exact scope.
- Do not add unnecessary safety gates, decision gates, hashes, CAS, or extra validation chains. Preserve existing mechanisms without expanding them. If one becomes necessary, explain the concrete need, insufficiency of existing mechanisms, impact, alternatives, and rollback before implementation; explicit user approval is required.

## Human gate sequence

```text
HG-00 Legacy real baseline
HG-01 Orchestration foundation/dependency choice, conditional
HG-02 Neutral declarative runtime real test
HG-03 Single-module real equivalence test
HG-04 Full-report shadow run
HG-05 Default-path switch approval
HG-06 Server and second-capability acceptance
```

## Resume instruction

A new Codex session should receive only:

```text
Read AGENTS.md and docs/implementation/RUNTIME_EXTRACTION_STATUS.md.
Confirm the current branch and latest commit.
Follow docs/CODEX_AUTONOMOUS_EXECUTION.md.
Continue from Current work package / Current human gate without repeating completed work.
```
