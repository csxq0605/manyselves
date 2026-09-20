# WP-00 Legacy Reporting Baseline

This document records the code and test boundaries used to compare the current
Legacy Reporting Runner with later declarative execution. It is not a second
implementation-status file and does not define runtime state.

## Production entry and call chain

```text
RunReportingWorkflowTool.__call__
→ ReportingRunController.start
→ ReportingService.prepare_run
→ ReportingService.run_prepared / _execute_locked
→ ReportWorkflowRunner.run
→ ReportWorkflowRunner._run_module_lanes
→ ReportWorkflowRunner._execute_module_lane
→ ReportWorkflowRunner._module_pipeline
→ run_module_review
```

The corresponding implementation is in:

- `manyselves/core/tools/reporting_tool.py`
- `manyselves/core/reporting/service.py`
- `manyselves/core/reporting/workflow.py`
- `manyselves/core/reporting/review_lifecycle.py`

The Legacy Reporting Runner remains the production default. WP-00 does not
change routing, scheduling, provider calls, output contracts, or recovery.

## Existing execution boundaries

| Boundary | Current owner | Compatibility evidence |
| --- | --- | --- |
| Agent invocation | `ReportWorkflowRunner._agent` delegates to `ReportingAgentRunner.run` | `tests/reporting/test_agent_runner.py` and `tests/reporting/test_agent_workflow.py` |
| Conversation identity | `ReportingAgentRunner._identity_key`, runtime/session caches, persisted conversation reference state | stable-identity, per-module auditor, and restart-state tests in `tests/reporting/test_agent_runner.py` |
| Tool exposure | `ReportingAgentRunner._tools`, current `ToolRegistry`, task-scoped tool lists | submission-schema, authoring-tool, and collaboration-tool tests |
| Structured contracts | `TaskEnvelope`, typed submission models, task-specialized submission schemas | `tests/reporting/test_agentic_models.py`, `test_submission_contracts.py`, and `test_agent_runner.py` |
| Module review | `run_module_review` and `request_module_revision` | lifecycle, resume, original-reviewer, and immutable-artifact tests in `test_agent_workflow.py` |
| Parallel lanes and recovery | `parallel_runtime.py`, `distributed_runtime.py`, and workflow lane recovery | parallel, distributed, module-auditor concurrency, and workflow recovery tests |
| Final output | workflow aggregation, final review, rendering, delivery verification and receipt | final-scope, deterministic-final-gate, rendering, output-verifier, and delivery tests |

## Recovery behavior that must remain unchanged

The following current behavior is frozen as compatibility scope:

- Natural language without the required structured submission is corrected in
  the same conversation.
- A schema error is returned to the same conversation for correction.
- A model response ending at `max_tokens` continues the same identity.
- A tool-slice boundary continues the current task and identity.
- Repeated continuation with no durable or conversational progress stops.
- Completed eligible tool results remain reusable through the existing result
  index.
- Agent identity and conversation/session reuse remain governed by the current
  identity mapping and persisted reference state.
- Same-run completed business results remain recoverable without redoing paid
  work.

Focused characterization tests for these boundaries include:

```text
test_max_tokens_continues_same_identity_without_submission_correction
test_tool_iteration_boundary_continues_same_identity_until_typed_submission
test_repeated_no_progress_continuation_stops_at_profile_harness_boundary
test_submission_correction_boundary_continues_until_typed_submission
test_reporting_agent_runner_stops_after_repeated_identical_submission_validation_error
test_reporting_identity_keeps_one_stable_session_across_workflow_turns
test_module_auditors_use_isolated_per_module_sessions_and_reuse_on_review
test_module_review_resume_continues_after_persisted_findings_without_reaudit
```

WP-00 adds no new Hash, CAS, validation chain, or recovery gate. Existing
mechanisms are observed and preserved without expansion.

## Stable semantic trace

`manyselves.runtime.semantic_trace` defines the comparison vocabulary only:

```text
workflow.started
action.started
conversation.created
agent.invoked
tool.invoked
contract.validated
branch.selected
action.completed
action.failed
workflow.completed
output.published
```

Events carry logical workflow/action/agent/conversation/contract/tool/branch
and output identities. They deliberately omit timestamps, random IDs, absolute
paths, token counts, model prose, and digest fields.

`tests/reporting/test_legacy_semantic_trace.py` adapts the current scripted
Legacy module review lane in memory:

```text
accepted module submission
→ original module auditor finding
→ original module author revision
→ same module auditor recheck
→ completed module output
```

The scenario runs in two independent temporary workspaces and must produce the
same ordered semantic records. The adapter is test-only and does not write to
the production event log or alter the Legacy workflow.

## Kernel prohibited dependencies

The Kernel may use Python and business-neutral runtime contracts. It must not
import:

- `manyselves.core.reporting`
- `manyselves.webapi`
- `manyselves.gui`
- concrete `manyselves.capabilities` implementations

`tests/kernel/test_import_boundary.py` checks Python imports under
`manyselves/kernel`. It does not scan domain words and it adds no runtime gate.

## Fake baseline and deferred final real-test separation

The automatic baseline uses only deterministic scripted runners and temporary
workspaces. It does not call a Provider or use real project material.

The single final real-test matrix, run only after all four migration stages are
complete, must include a Legacy baseline using:

- the unchanged Legacy Reporting Runner;
- one fixed representative project workspace and input set;
- the operator-selected real Provider/model and recorded configuration;
- a saved Run ID;
- module, review, Cross, Chief/final, output and delivery artifacts from that
  same Run;
- the actual usage/cost record for that Run.

No intermediate real run is required after WP-00. The Legacy run is deferred
and paired with the completed declarative path in the single final real test,
because both transmit project data to a Provider and must verify the delivered
DOCX in the operator's environment.
