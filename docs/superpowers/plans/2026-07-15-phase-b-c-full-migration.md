# Phase B/C Full V2 Migration Plan

> Execute with test-driven development. Customer files under `work/` are read-only; real tests copy them into pytest temporary `Inputs/` directories.

## Goal

Extend the audited Phase A `2.4` vertical slice into the complete V2 report workflow: all five modules, real parallel drafting, cross-module review, chief editing, complete Chapter 3 output, and governed Skill candidate publication/rollback.

## Task 1: Lock the handoff taxonomy

**Files:** `autoreport/core/reporting/taxonomy.py`, `tests/reporting/test_taxonomy.py`

1. Add a failing test for exact module titles and representative `2.1`–`2.5` leaf titles from the V2 handoff.
2. Replace placeholder titles with the handoff's 37 leaf submodules.
3. Run `tests/reporting/test_taxonomy.py` and the reporting suite.

## Task 2: Route evidence for all five modules

**Files:** `autoreport/core/reporting/mappers/s2_1.py`, `s4_4.py`, `s4_6.py`, mapper tests.

1. Add failing tests proving that S2-1 documentation/maintenance facts, S4-4 load/harmonic/thermal/protection/site facts, and S4-6 summary rows route to their exact leaf submodules.
2. Keep S4-6 derived conclusions at low confidence with `needs_confirmation=True`.
3. Preserve source coordinates, evidence IDs, photo references, and WPS image relationships.
4. Run mapper, coverage, and real-workbook tests.

## Task 3: Package Skills and draft every module

**Files:** `autoreport/core/reporting/skills/`, `workers/generic.py`, `workers/module_24.py`, worker tests.

1. Add failing tests requiring an approved, versioned Skill for every leaf submodule.
2. Add general Skills for modules `2.1`, `2.2`, `2.3`, and `2.5`; retain the specialized `2.4` workers.
3. Implement a generic evidence-bound worker that emits fact, cautious conclusion/risk, and recommendation claims without fabricating missing facts.
4. Require every claim to carry the responsible Skill version and exact evidence scope.

## Task 4: Prove true five-module parallelism

**Files:** `autoreport/core/reporting/workers/orchestrator.py`, `service.py`, concurrency tests.

1. Add a failing barrier/timing test in which sequential execution cannot pass.
2. Dispatch five independent module tasks through `asyncio.gather`/thread offload.
3. Make output ordering deterministic (`2.1` through `2.5`) despite concurrent completion.
4. Persist per-module execution metadata and independent draft artifacts.

## Task 5: Cross-module review and Chief Editor

**Files:** `review/cross_module.py`, `editing/chief.py`, models, tests.

1. Add failing tests for contradictory metrics, duplicated findings, action conflicts, and immutable evidence-bound claims.
2. Produce structured cross-module issues and route each issue only to the responsible module.
3. Let the Chief Editor reorder/deduplicate narrative and build Chapter 3, but reject edits that alter protected claim facts or citations.
4. Preserve the complete review and revision history in `ReportState`.

## Task 6: Render the complete report

**Files:** `models.py`, `rendering/docx.py`, `service.py`, rendering/integration tests.

1. Add failing tests for complete `1`, `2.1`–`2.5`, `3.1`, and `3.2` structure.
2. Extend `ReportState` with executive overview, risk summary, prioritized actions, cross-module issues, model/rule/Skill versions, and review history.
3. Render all five approved module drafts and Chapter 3 into the handed-off DOCX template.
4. Emit the full artifact contract under `Work/` and `Outputs/`.

## Task 7: Govern Skill candidates

**Files:** `autoreport/core/reporting/skills/governance.py`, governance models/storage/tests.

1. Add failing tests for candidate creation, isolated regression evaluation, publish rejection, successful publish, and rollback.
2. Use immutable semantic versions and append-only version history.
3. Block candidate publication unless all regression cases pass and the approved baseline does not regress.
4. Persist candidate, evaluation, publication, and rollback records without mutating packaged baselines.

## Task 8: Final acceptance

1. Run the complete reporting suite and full repository suite excluding the known live-LLM test.
2. Run scoped Ruff and `git diff --check`.
3. Build sdist and wheel offline.
4. Copy the three customer workbooks into a pytest temporary workspace and run full five-module E2E.
5. Convert the final DOCX to PDF with the configured system Chinese fonts; inspect all pages and verify embedded fonts, no clipping, no overflow, traceable images, and a readable review appendix.
6. Update the acceptance matrix with exact counts, hashes, limitations, and commands.
