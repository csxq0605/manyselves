# Bundled capability: power-distribution reports

This document is the detailed contract for the Agent team currently bundled with
Manyselves. The capability is domain-specific; the Manyselves runtime is not.

## Team and orchestration

The user communicates only with Main. Main creates a typed request, while Python
orchestration owns dependencies and state transitions. Task-scoped Agents cover
intake, evidence normalization, coverage, modules 2.1–2.5, responsibility audit,
cross-module review, Chief Editor integration, delivery, and Skill governance.

Professional acceptance belongs to the Evidence Auditor and Cross-module
Reviewer. Main cannot accept an open typed blocking issue. There is **no fixed business-round limit**:
Main decides whether new evidence is converging enough to
revise again, request input, or stop incomplete. 业务返工没有固定轮数。

## Evidence decisions

With `missing_evidence_policy: ask`, insufficient evidence creates a durable
`EvidenceDecisionRequest`:

- `supplement` rescans evidence in the same run;
- `draft` continues with explicit uncertainty;
- `skip` preserves the fixed module tree and marks unsupported submodules not
  evaluated;
- `stop` ends incomplete without a success artifact.

The requests survive process restarts. The other policies are `block`, `skip`,
and `draft`.

## Evidence boundary

Customer inputs live under `Inputs/`. Project reference material lives under
`Knowledge/`. Every supported file below `Knowledge/` is eligible for search and
`R-*` citation without directory-name allowlists or filename matching. Reference
and web sources support interpretation but cannot become customer-site facts
(`E-*`). Arbitrary spreadsheet rows do not make a submodule ready.

## Delivery and revision

Every complete delivery publishes an immutable `ReportVersion` with restorable
module submissions, evidence/claim/source snapshots, delivery receipt, template
provenance, exact loaded Skill provenance, and context-only
`AgentSessionSummary` snapshots.

Post-delivery feedback restores a baseline version, reruns only the responsible
specialist plus responsibility audit, then cross-reviews, recomposes all five
modules, renders a complete DOCX, and publishes a child version. Out-of-scope
changes become a reviewable `scope-expansion request`.

## Skill evolution

Report revision does not automatically change a Skill. Explicit capability
evolution follows:

`FeedbackRecord → SkillCandidate → EvaluationResult → confirmation → SkillVersion`

Project Main owns project-local publication. The `Product Skill Maintainer` owns
product-wide publication. Active versions are selected per `skill_id`; later
runs resolve **packaged → product → project** precedence. A report version freezes
the exact Skill IDs, versions, scopes, and hashes it used.

## Runtime artifacts

Runtime access is capability-scoped rather than prompt-only. Agents use
`open_artifact` and `search_text` with bounded pages/matches; large tool results
and compacted transcripts are returned as signed, workflow/session-bound opaque
references and can be reopened with `open_tool_result`. Raw `.manyselves` paths
remain unavailable through the general `read` tool. The default active working
set targets 36,000 estimated tokens; compaction writes one recoverable,
content-deduplicated checkpoint instead of repeatedly reinserting removed text.

DOCX tables, XLSX rows, PDF pages, and text are parsed through format-aware
adapters. Image metadata explicitly reports `visual_verified: false`; dimensions
or file format are never represented as visual inspection. Unsupported binary
formats return a structured unsupported/error state rather than silently decoding
bytes as UTF-8.

Every tool call is normalized into a semantic `ok`, `failed`, or `blocked`
outcome. Reporting entry points, `submit_result`, and `report_blocked` are
terminal: AgentLoop publishes a deterministic final status and does not ask the
provider to narrate success afterward. A run can be `completed` only after the
shared verifier confirms current-run, in-workspace, non-empty/openable artifacts
and validates the delivery receipt when present. Token and request accounting is
written once per provider attempt, including retries and guard calls. Working
memory and usage budgets may compact, replan, or block; they never decide that a
task is complete.

Workflow collaboration is owned by one router per workflow. Sessions are keyed
by workflow, Agent identity, and session ID; ambiguous same-identity peer queries
must specify `target_session_id`. Research, revision, gap, and blocked notices
are indexed as artifact references and router subscriptions are removed when the
workflow closes.

The offline runtime verification uses deterministic fake providers with the real
AgentLoop and MessageBus. It does not make paid-provider calls.

```text
Work/manifest.json
Work/evidence.jsonl
Work/coverage.json
Work/photo-manifest.json
Work/report-state.json
Work/runs/<run-id>/workflow-state.json
Work/runs/<run-id>/modules/*.json
Work/runs/<run-id>/reviews/*.json
Work/runs/<run-id>/ledgers/sources.json
Work/runs/<run-id>/decisions/*.json
Work/runs/<run-id>/session-summaries/*.json
Work/runs/<run-id>/template-provenance.json
Work/runs/<run-id>/delivery-receipt.json
Work/report-versions/<version-id>/version.json
Outputs/Modules/2.1.md ... 2.5.md
Outputs/Reviews/full-review.json
Outputs/Reports/配电安全专家咨询报告.docx
Outputs/Reports/render-log.json
Capabilities/skills/{feedback,candidates,evaluations,versions}/...
ProductCapabilities/skills/{feedback,candidates,evaluations,versions}/...
.manyselves/usage/<run-id>.jsonl
```
