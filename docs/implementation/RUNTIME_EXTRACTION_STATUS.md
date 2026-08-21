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

- Current work package: `WP-01/WP-11 architecture completion audit reopened`
- Last completed vertical slice: the packaged module Cohort is the production top-level Subworkflow; YAML owns preparation, five Parallel branches, Join, reduction, and failed-branch-only retry while each branch delegates the complete current Lane lifecycle
- Current branch and latest committed audit slice: `agent/declarative-runtime-implementation`; `d8dab30` (`WP-02/WP-09: persist nested runtime events`); the production Cohort slice is this document's commit
- Current migration stage: `file-defined Reporting workflow migration`; the prior four-stage completion claim is superseded by the live-code audit below
- Final real-test status: `not ready`; no real test should run until the true file-defined Reporting path, one authoritative runtime state, generic interaction/output execution, and capability-neutral API/UI are complete
- Next automatic action: decompose each complete-Lane Capability adapter into a file-defined authoring Lane and the existing module-review workflow, retaining the current recovery, preflight, correction, continuation, Conversation, and artifact implementations behind smaller Capability adapters until their equivalence is proved

## Reopened architecture completion audit

The 2026-08-21 live-code audit found that the previous completion record proved
several useful vertical slices but did not prove the requested target
architecture:

- the selectable Reporting runner still subclasses `ReportWorkflowRunner`; its
  production module entry is now the packaged Cohort, but each branch still
  delegates one complete current Lane lifecycle to a Capability adapter;
- the packaged detailed review Lane is file-owned and executable, but the full
  production authoring/preflight/review lifecycle is not yet decomposed into it;
- `RecoveryController` is not yet part of the production generic Agent execution
  chain;
- the current Cross implementation and combined Render/Delivery implementation
  remain coarse Capability Tools rather than fully file-defined action graphs;
- application and React projections require a final audit for remaining
  Reporting-specific dispatch or presentation decisions.

Therefore all prior “complete” entries below are retained as historical commit
and test evidence, not as a current architecture-completion assertion. Legacy
Reporting remains the default and no real Provider, browser, project, or server
test is requested while this audit is open.

## Required startup checks

Codex must begin by running and recording:

```bash
git status -sb
git branch --show-current
git log -1 --oneline
```

The plan baseline must contain the autonomous execution commits and this status file. Codex must not implement on `main` or directly on `feature/react-fastapi-manyselves`.

## Completed commits

- `WP-07/WP-08: run production modules through the file cohort` — the top-level workflow invokes the packaged module Cohort as a Subworkflow; its YAML owns preparation, fixed five-branch Parallel/Join, and reduction; production branches delegate complete current Lane semantics, successful siblings remain embedded in one parent Run, only failed branches retry, the coarse `run-reporting-module-work` Tool definition is removed, and the existing legacy barrier finalizer is reused from one extracted boundary without copying or adding hash/CAS logic (this commit)
- `WP-02/WP-09: persist nested runtime events` — nested Subworkflows now emit standard workflow/action/output lifecycle events through the parent Runtime Host sink; the selectable Reporting runner persists top-level and tail events in the exact Run directory, without a child state directory (this commit)
- `WP-08/WP-09: run reporting stages in one parent state` — `distribution-reporting.yaml` now branches partial/full work itself and invokes the packaged tail as a recoverable Subworkflow; the selectable declarative runner enters one `WorkflowRuntimeHost`, persists only the exact run identity, removes the coarse tail Tool, and resumes inside a failed tail without replaying completed module work or prior tail stages (this commit)
- `WP-06/WP-09: persist failed subworkflow progress` — a failed nested effect now returns its child WorkflowState to the parent Runtime Host before the parent failure transition is persisted; a same-Run retry reuses completed child Actions and reruns only the failed Action without creating a child Run directory (this commit)
- `WP-09: compile the reporting tail from files` — the production Workflow YAML owns the ordered Cross, Chief, Final, and current combined Render/Delivery actions; all four internal Tools are file-declared, the builder only loads the Capability Registry, and execution/recovery moved from the persistence-owning control executor to `WorkflowRuntimeHost` under the parent Run identity (this commit)
- `WP-07/WP-08: compile reporting modules from files` — the packaged reusable Lane YAML owns every Tool, Conversation, Agent, branch, and End action; the packaged Cohort YAML owns all five Parallel branches and Join; declared typed parameters preserve module/lifecycle/revision IDs, all internal Python Tools are file-declared, and Python now only specializes runtime values and binds implementations (this commit)
- `WP-07/WP-08: recover module lanes in one runtime state` — both detailed Reporting paths execute effects through `WorkflowRuntimeHost`; Lane states are embedded in parent Parallel branch state, only the parent Run is persisted, and a failed Cohort retry uses a generic pure branch-reset transform so completed sibling branches are not replayed (this commit)
- `WP-06: keep parallel and child state in one runtime run` — Runtime Host executes Parallel/Join/Subworkflow effects, honors declared concurrency, and stores branch/child WorkflowState snapshots inside the parent state; tests prove no branch or child Run directories are created (this commit)
- `01033d3` — `WP-06: reduce basic control flow in the stateless kernel`; moves If/ConditionGroup/Goto/ForEach decisions and bounded back-edge state into the pure reducer; the neutral parameter fixture now runs Tool, Contract, branch, Agent, Conversation, and Goto through `WorkflowRuntimeHost` instead of the persistence-owning control executor
- `6fde929` — `WP-02: execute effects outside the stateless kernel`; adds the pure Start/Success/Failure reducer and ExecuteAction effects; a Runtime Host now executes effects, persists one WorkflowState, appends standard workflow/action/output events, and reuses a completed same-Run result without replay
- `197cca6` — `WP-02/WP-11: execute declared interactions and outputs`; adds Interaction/Output definition kinds and references, compiles RequestInput/PublishResult, persists a waiting WorkflowState, resumes through a pure validated state transition, and publishes the declared result without Capability-specific logic
- `6cd2688` — `WP-01/WP-11: bind capability runtimes from definitions`; adds explicit Capability entry points and trusted Python runtime-factory references, dispatches start/input/run/output/cost through a generic binding catalog, and moves all ReportRequest/UserSupplement/snapshot translation into the Distribution Reporting adapter
- `fb7f857` — `WP-01/WP-11: discover production capability bundles`; reopens the architecture audit, adds file-based Capability discovery and workflow ownership, removes the neutral fixture from the installed/API product surface, and retains it under `tests/fixtures` as a complete Markdown/YAML/Schema runtime proof
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
- `WP-06: execute declarative control flow` — neutral If/ConditionGroup/Goto/ForEach/Parallel/Join/Subworkflow actions, explicit back-edge limits, persisted loop cursor, and one authoritative WorkflowState (this commit)
- `WP-07: migrate one declarative module lane` — one explicit Reporting-owned module review definition, current Pydantic Agent contracts, current packaged Agent prompts and Tool declarations, stable Author/Auditor Conversation Keys, conditional revision, and exact scripted Legacy semantic Trace equivalence (this commit)
- `WP-08: join the declarative module cohort` — fixed five-Lane Reporting Parallel/Join, optional declared concurrency limit, completion reuse through each Lane WorkflowState, sibling drain through typed Reporting outcomes, and all-complete reduction without new barrier hashes or CAS (this commit)
- `WP-09: sequence current reporting tail declaratively` — first WP-09 slice: Reporting-owned Cross → Chief → Final → Delivery Tool adapters over the current implementations, ordered neutral actions, stage-marker reuse, and failed-stage continuation state (this commit)
- `WP-09: compare current and declarative tail traces` — standard semantic Trace adapter for the ordered Cross, Chief, Final, and Delivery stage boundary, compared against direct execution of the same current stage implementations (this commit)
- `WP-09: complete the declarative reporting tail` — completes WP-09 through Reporting-owned adapters: the current Cross owner pipelines, Chief/Final chapter lanes, Render, Delivery, completion-marker reuse, and same-run failed-stage continuation now execute behind an explicit declarative tail while Legacy remains the default (this commit)
- `WP-10: package the distribution reporting capability` — packages the 19 current Reporting Agent identities, Task/Workflow/Contract/Tool/Recovery indexes, and executable Reporting adapters under `manyselves.capabilities.distribution_reporting`; the old packaged-Agent loader remains a compatible import, Kernel import boundaries remain intact, and no new Gate definition is introduced (this commit)
- `WP-11: expose generic workflow projections` — adds the eight planned generic FastAPI projections for Capability, Workflow, input schema, Run, input, Output, and Cost; current Reporting start/resume/decision behavior remains behind a thin Capability adapter, and generic Outputs do not add or expose new hash/CAS logic (this commit)
- `WP-11: add the generic Run workspace` — adds a project-scoped React Capability/Workflow selector, JSON input, Run status, Output, Cost, and continuation-input view over the generated generic API; the existing Reporting-specific store/view remains a compatibility adapter and no Reporting identities or SHA-based delivery checks enter the generic UI (this commit)
- `WP-12: execute a second neutral capability` — packages and executes `parameter-adjustment` through Tool, Contract, condition, run-scoped Conversation, Capability-owned Agent, Goto, and output actions; the package contains no Reporting identities, Gate definitions, hashes, CAS, or Reporting imports (this commit)
- `WP-12: project the neutral capability through Run` — exposes both capabilities through the same generic Workflow/Run API, persists and projects neutral Run state, generalizes Output to artifact-or-value without exposing legacy digests, and lets the React Run Workspace select and display the neutral result (this commit)
- `WP-09: connect the explicit declarative Reporting path` — closes the completion-audit gap with a top-level executable Reporting definition, persisted declarative module-stage and tail orchestration, explicit `report-declarative-*` selection through the generic Workflow API, and same-run engine recovery by readable run identity; the existing Reporting API and Controller default remain Legacy (this commit)
- `WP-11: poll generic Run completion` — keeps the generic Run projection live while a background run is active, then refreshes Output and Cost once it reaches a terminal state; completed neutral runs still do not expose Reporting continuation controls (this commit)

## Tests actually run

- Production-Cohort Characterization first failed because the top-level plan still began with the coarse Tool; after the YAML change it compiled as Cohort Subworkflow → If → Tail Subworkflow → End. The first execution then failed because the Runtime Context lacked the compiled Cohort, which was corrected by binding that child plan and its file-declared branch Tools.
- Failed-branch Characterization proved a two-module production adapter attempt drained its sibling, persisted the child branch states inside the parent Run, then retried only failed module `2.2`: `1 passed`. The legacy finalizer extraction retained the existing all-ready and failure-barrier behavior: `2 passed`.
- Production Cohort affected Reporting runner/Cohort/Lane/tail, current module concurrency, Capability package, Runtime Host, and Kernel import-boundary selection passed: `29 passed`. The wheel rebuilt and contains the top-level/Cohort YAML plus `prepare-module-cohort.yaml`, and no longer contains `run-reporting-module-work.yaml`. Ruff passed for the changed focused paths and `git diff --check` passed. No full regression or real runtime test ran.
- Nested-event Characterization first failed because the child workflow had no events; after implementation the generic Host emitted the complete child lifecycle and the production Reporting entry persisted the ordered tail lifecycle beside the one authoritative state: `2 passed` focused, `10 passed` affected. Ruff and `git diff --check` passed. No full regression or real runtime test ran.
- Top-level file-graph Characterization initially failed because the packaged workflow still used a coarse tail Tool; after implementation its compiled kinds are Tool → If → Subworkflow → End and the declared child Workflow is resolved.
- One-parent focused cases passed for partial module delivery, failed module retry, complete module→tail execution, and failed-tail nested recovery. The latter proved module work called once, Cross reused, Chief retried, and one Run directory: `5 passed` with the packaged compiler case.
- Affected declarative Runner/Tail/Lane/Cohort, Capability package, Runtime Host, control flow, service runner selection, and Kernel boundary selection passed: `34 passed` (`46 deselected`); the direct package/runner/tail/host selection passed: `20 passed`. Ruff and `git diff --check` passed. No full regression or real runtime test ran.
- Failed-Subworkflow Characterization initially failed because the parent contained no child state; after implementation the child first Action remained completed, only the failed second Action retried, and the parent produced the expected output: `1 passed`.
- Runtime Host plus affected sequential/basic-control selection passed: `22 passed`; Ruff and `git diff --check` passed. No full regression or real runtime test ran.
- File-defined tail and one-state recovery Characterization initially failed because the packaged tail had no actions and persisted `run_id--reporting-tail`; after migration, exact trace equivalence, completion-marker reuse, failed-stage continuation, one Run directory, and packaged-definition ownership passed: `5 passed`.
- Tail/runner/package, Runtime Host, sequential Runtime, and Kernel import-boundary affected selection passed: `27 passed`; Ruff and `git diff --check` passed. No full regression or real runtime test ran.
- File-defined Lane/Cohort Characterization initially failed because both packaged workflow files had empty action lists; after migration the focused packaged definitions plus live Lane/Cohort execution selection passed: `11 passed`.
- File-definition/Registry/Compiler, Runtime Host, packaged Reporting, Lane/Cohort semantic behavior, generic Workflow projection, and Kernel import-boundary affected selection passed: `54 passed`; Ruff and `git diff --check` passed. The wheel rebuilt and contains both executable Workflow YAML files and the new internal Tool declarations. No full regression or real runtime test ran.
- Reporting one-state recovery Characterization replaced the old shadow-directory assertion and initially failed because the parent branch snapshot stopped at Join as `running`; after implementation, a same-Run retry invoked only failed module `2.2`, reused the other four completed branches, completed the parent Run, and left exactly one Run directory: `1 passed`.
- Reporting one-state affected Lane, Cohort, Runtime Host, Interaction/Output, and basic-control selection passed: `18 passed`; Ruff and `git diff --check` passed. No full regression, Provider, browser, or server test ran.
- Stateless Parallel/Join/Subworkflow focused Host selection passed: `4 passed`; affected new/legacy control, sequential, neutral Capability, and Kernel boundary selection passed: `25 passed`; Ruff passed after import normalization.
- Stateless basic-control focused Host plus neutral Tool/Agent/If/Goto selection passed: `5 passed`; the affected legacy/new sequential and control-flow comparison plus Kernel boundary selection passed: `23 passed`.
- Stateless-kernel Characterization initially failed collection because StartWorkflow and the Runtime Host did not exist; after implementation, pure non-mutating transition and effect/persistence/event/same-Run reuse tests passed: `2 passed`.
- Stateless sequential affected Runtime, Workflow, Definition, neutral Capability, and Kernel import-boundary selection passed: `50 passed`; Ruff passed for all changed paths.
- Interaction/Output Characterization initially failed collection because the resume primitive and definition/action vocabulary did not exist; after implementation, the focused file-definition and wait/resume/publish selection passed: `2 passed`.
- Interaction/Output affected Definition, sequential/control-flow Runtime, Recovery, Agent/Tool adapter, neutral Capability, and Kernel import-boundary selection passed: `59 passed`; Ruff passed for all changed Kernel and test paths.
- Runtime-binding Characterization initially failed collection because the generic binding module did not exist. The synthetic Capability deliberately used different Capability and entry Workflow IDs; after implementation, focused binding, packaged Reporting definition, and projection tests passed: `11 passed`.
- Runtime-binding affected Definition, Runtime adapter, synthetic application binding, packaged Reporting, generic projection, and HTTP route selection passed: `38 passed`; Ruff passed for every changed Python/test path.
- Reopened-audit Capability Catalog Characterization initially failed collection because `CapabilityCatalog` did not exist; after implementation, the Catalog, production-only bundle boundary, relocated neutral fixture, generic Reporting projection, and generic HTTP non-exposure selection passed: `11 passed`.
- Reopened-audit affected Definition, Distribution Capability, neutral fixture, generic Reporting route, and Kernel import-boundary selection passed: `30 passed`; Ruff passed for every changed Python/test path.
- The wheel rebuilt successfully and contains only the installed `distribution_reporting` Capability; the `parameter_adjustment` definition graph remains executable from `tests/fixtures` and is not shipped in the application package.
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
- WP-06 Control Flow Characterization: initially failed collection because `ControlFlowWorkflowExecutor` did not exist, then `7 passed` after implementation.
- WP-06 affected Kernel/Runtime selection: `54 passed`; affected Reporting definition/config and Kernel boundary selection: `7 passed`.
- One initial WP-06 affected command named a nonexistent `tests/runtime/test_state_store.py` and collected no tests. A subsequent combined collection exposed two same-basename test modules; the new Conversation test was renamed, after which the recorded affected selections passed.
- WP-07 Legacy semantic Trace Characterization passed before implementation: `1 passed`.
- WP-07 declarative module Lane Characterization initially failed collection because `manyselves.core.reporting.declarative_module_lane` did not exist; the new Lane plus Legacy Trace focused selection then passed: `3 passed`.
- WP-07 named multi-variable Tool input Characterization initially recorded `2 failed, 7 passed`; the additive single-or-named binding implementation then passed its focused selection: `9 passed`.
- WP-07 affected declarative/Legacy module Lane, current review lifecycle and Conversation identity, Legacy Agent adapter, Kernel sequential/control-flow, and import-boundary selection: `27 passed`.
- WP-08 declarative cohort Characterization initially failed collection because `manyselves.core.reporting.declarative_module_cohort` did not exist.
- WP-08 concurrency-limit Characterization initially failed because `ParallelAction` did not accept `max_concurrency`; the focused Kernel case then passed: `1 passed`.
- WP-08 focused five-Lane all-ready Join, concurrency limit, completed-Lane reuse, sibling drain, and failed-cohort projection selection: `3 passed`.
- WP-08 affected declarative cohort/Lane, current Legacy module concurrency, ready supervisor and recovery state, Kernel sequential/control-flow, and import-boundary selection: `29 passed`.
- WP-09 Reporting-tail Characterization initially failed collection because `manyselves.core.reporting.declarative_reporting_tail` did not exist; its order, completion-marker reuse, and failed-stage continuation focused tests then passed: `3 passed`.
- WP-09 first-slice affected declarative tail, current Cross owner/specialization, selected Chief/Final chapter lanes, current Delivery materialization, Kernel sequential workflow, and import-boundary selection: `28 passed`.
- WP-09 tail Trace Characterization initially failed because the declarative tail had no Trace adapter; the direct-current/declarative standard semantic Trace comparison selection then passed with the complete tail focused set: `4 passed`.
- WP-10 Capability-package Characterization initially failed collection because `manyselves.capabilities` did not exist; the packaged definition graph, current-Agent projection, Legacy template-corpus equivalence, compatibility loader, and executable adapter selection then passed: `5 passed`.
- WP-10 affected packaged Agent config/prompts, declarative module Lane/Cohort/tail, and Kernel import-boundary selection passed: `42 passed`.
- WP-10 wheel build succeeded, and the built wheel contains the Capability entry file plus its Agent, Task, Contract, Tool, Workflow, and Recovery definition assets.
- WP-11 generic projection Characterization initially failed collection because `manyselves.application.workflow_projection` did not exist; pure Capability/Workflow/schema/Run/Output/Cost/command projections plus OpenAPI path coverage then passed: `4 passed`.
- WP-11 first-slice affected generic routes, complete OpenAPI contract, Distribution Reporting package, and selected current Reporting start/query/cancel compatibility selection passed: `26 passed`.
- The canonical OpenAPI artifact and generated React TypeScript schema were refreshed; `npm run check:api` passed.
- WP-11 React Characterization initially failed because the generic Workflow API and Run Workspace modules did not exist; the focused API, operable Workspace, Sidebar, and route selections then passed: `10 passed` total across the selected files/nodes.
- Targeted ESLint passed for all WP-11 React paths; TypeScript `--noEmit` and the production Vite build passed. No browser/server or full frontend test regression was run.
- WP-12 second-Capability Characterization initially failed collection because `manyselves.capabilities.parameter_adjustment` did not exist; the complete definition graph and both direct-finish and Agent/Goto execution cases then passed: `3 passed`.
- WP-12 generic-projection Characterization initially recorded `4 failed, 1 passed` because the facade still projected only Reporting; the second Capability, async start, neutral Run state, value Output, and Reporting artifact Output selection then passed with the Capability tests: `8 passed`.
- WP-12 affected generic Reporting and neutral HTTP Run routes passed: `2 passed`; the complete OpenAPI contract selection passed: `14 passed`. The OpenAPI route-dependency Characterization was made independent of an existing built frontend static `Mount`; no production route behavior changed.
- WP-12 focused React Workflow API and Run Workspace selection passed: `3 passed`; generated API drift check, targeted ESLint, TypeScript `--noEmit`, and the production Vite build passed. The neutral completed run does not offer Reporting continuation input.
- Program completion audit found that the declarative Reporting components were not connected to a complete selectable entry. Characterization initially failed collection because `declarative_reporting_runner` did not exist; the executable packaged workflow, module-stage execution/retry, service runner selection, controller default/explicit selection, tail continuation, generic API, and package adapter selection then passed: `29 passed`.
- The directly affected Reporting service boundary/completion/input-snapshot and Kernel import-boundary selection passed: `55 passed`. No Provider was called. The new selection uses `report-declarative-*`, adds no mode-validation file, Gate, hash, or CAS, and leaves `report-*` on the current Legacy default.
- WP-11 completion-polling Characterization initially failed because an active Run was queried only once; the focused Run Workspace/API selection then passed: `4 passed`. Targeted ESLint with zero warnings and TypeScript `--noEmit` passed.
- Final affected Kernel/Runtime/Capability selection: `67 passed`.
- Final affected declarative/Legacy Reporting, recovery, background execution, service boundary, completion, and input-snapshot selection: `165 passed`.
- Final affected generic projection, OpenAPI, Legacy start, declarative Reporting route, and neutral Capability HTTP selection: `22 passed`.
- Final frontend generated-API drift check passed; focused Run Workspace/API selection: `4 passed`; targeted ESLint, TypeScript `--noEmit`, and the Vite production build passed.
- Final source and wheel distributions built successfully. The wheel contains both `distribution_reporting` and `parameter_adjustment` Capability definitions and assets.
- Final incremental scan found no newly added hash, digest, SHA, or CAS logic in the Kernel, Runtime, Capability packages, declarative Reporting adapters, or generic projection. Kernel business-term inspection found no Reporting implementation dependency.
- `uv lock --check` and `git diff --check` passed.
- Ruff passed for every changed Python and test path. `git diff --check` passed.
- No real Provider was called. No full regression was rerun after the user's instruction.

## Research decisions

- R-01 Microsoft Agent Framework decision: `reference`.
- R-02 LangGraph decision: `reference`.
- WP-06 implementation choice: the existing lightweight internal Compiler/Executor, using current dependencies only.
- No new production orchestration dependency is approved or added.
- `jsonschema>=4.23,<5` was added only to execute the required generic JSON Schema Contract Adapter; it is not an orchestration dependency and is not used for runtime gates, security checks, hashes, or CAS.
- WP-07 added no hash or CAS implementation. Its scripted recheck delta contains only the changed assigned narrative, so the new path does not create unchanged-content fingerprints; existing Legacy compact-delta behavior remains untouched.
- The original WP-08 standalone detailed-review Cohort uses its WorkflowState as the authoritative result. It does not call the Legacy hash-bearing `WorkflowReducer`, does not write a new barrier artifact, and publishes its output only after all five typed Lane outcomes are completed.
- The production Cohort reuses the existing `ReportWorkflowRunner._finalize_module_lanes` compatibility boundary, which contains the pre-existing `WorkflowReducer.write_module_barrier` call and historical completion metadata. The extraction moved that code without copying, expanding, or adding any hash/CAS calculation; generic branch retry depends only on WorkflowState outcomes.
- WP-10 keeps the Capability `gates/` index intentionally empty because the migrated path has no new acceptance or decision Gate. Existing Reporting recovery behavior is indexed without new attempt limits, hashes, CAS, or validation chains.
- The explicit Reporting engine selection is routing, not a new acceptance or safety Gate: existing Reporting starts omit the parameter and remain `legacy`; only the generic declarative Capability start passes `declarative`. The readable run prefix preserves the same selection across resume without a new metadata verifier, digest, or CAS record.

The isolated POCs are recorded in `docs/research/DECLARATIVE_RUNTIME_LANDSCAPE.md`. The production path uses the current dependency set and a lightweight internal Compiler/Executor.

## Known blockers

- None. The audit gaps are implementable with the current dependencies and
  existing public compatibility adapters.

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

The original four-stage commit sequence exists, but its automatic completion
claim is superseded by the reopened architecture audit above. The previous
[`FINAL_RUNTIME_REAL_TEST_HANDOFF.md`](FINAL_RUNTIME_REAL_TEST_HANDOFF.md) is
historical and must not be executed until this status returns to `ready`.

## Resume instruction

A new Codex session should receive only:

```text
Read AGENTS.md and docs/implementation/RUNTIME_EXTRACTION_STATUS.md.
Confirm the current branch and latest commit.
Follow docs/CODEX_AUTONOMOUS_EXECUTION.md.
Continue from Current work package / Current migration stage without repeating completed work.
```
