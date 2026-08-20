# Runtime Extraction Implementation Status

> This is the single handoff record for the long-running Codex implementation program.
>
> Runtime execution state, provider traces, and product logs do not belong in this file.

## Program

- Repository: `csxq0605/manyselves`
- Plan base branch: `agent/declarative-runtime-plan`
- Intended implementation branch: `agent/declarative-runtime-implementation`
- Intended PR: one long-lived Draft PR targeting `agent/declarative-runtime-plan`
- Execution protocol: [`docs/CODEX_AUTONOMOUS_EXECUTION.md`](../CODEX_AUTONOMOUS_EXECUTION.md)

## Current position

- Current work package: `WP-00`
- Last completed work package: `none`
- Current human gate: `none`
- Human gate result: `not_started`
- Next automatic action: create or switch to `agent/declarative-runtime-implementation`, establish the legacy characterization and semantic trace baseline, then continue automatically until `HG-00`

## Required startup checks

Codex must begin by running and recording:

```bash
git status -sb
git branch --show-current
git log -1 --oneline
```

The plan baseline must contain the autonomous execution commits and this status file. Codex must not implement on `main` or directly on `feature/react-fastapi-manyselves`.

## Completed commits

- None. Planning and handoff documents are on the plan branch; implementation has not started.

## Tests actually run

- None for implementation. The planning branch contains documentation-only changes.

## Open research decisions

- Whether to adopt, adapt, or only reference Microsoft Agent Framework Declarative Workflow remains undecided.
- Whether LangGraph or an internal lightweight compiler/runtime should be used remains undecided.
- No new production orchestration dependency is approved.

These questions do not block `WP-00`. They must only trigger `HG-01` if a later POC shows that a production dependency or long-term public interface decision is necessary.

## Known blockers

- None at handoff time.

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
