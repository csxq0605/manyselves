# Phase 1 implementation status

Updated: 2026-08-04. Active implementation branch: `codex/phase1-light-web`.

This file separates implemented code, automated evidence, and external production acceptance. “Implemented” never means a reviewer or production pilot was fabricated.

| Plan | Scope | Implementation | Automated gate |
|---|---|---|---|
| 01 | Runtime foundation and protected boundary | Complete | Gate A evidence complete; protected-core freeze passes |
| 02 | FastAPI, SSE, control lease, stable API | Complete | Gate B accepted and regression-covered |
| 03 | React project/file/editor/preview workspace | Complete | Gate C browser/unit/build evidence passes |
| 04 | React conversations, agents, reporting, settings | Complete | Gate C full parity evidence passes |
| 05 | Secure Electron client, Compose, backup/restore | Complete | Gate D package/E2E/deployment evidence passes |
| 06 | Compatibility, recovery, security, deployment verifier, release record | Complete in repository | Gate E automated checks pass except the external image scanner could not be downloaded; clean-host pilot and row-by-row product acceptance remain external |

## Light-web redesign and stabilization (Plans 07-11)

This is the authoritative handoff checkpoint for the active redesign cycle. A task is not marked complete until its code is committed and its listed verification/review gates have passed.

| Plan / task | Scope | Status | Evidence / remaining work |
|---|---|---|---|
| 07 | Cookie session authentication and simple administrator login | Complete | Implemented and committed before the Plan 08 cycle; login is the URL entry page and the browser no longer asks users for an access token |
| 08 / Task 1 | Project metadata and project API | Complete | Implemented, tested, committed, independent review PASS |
| 08 / Task 2 | Codex-light shell, project navigation, control-lease lifecycle | Complete functionally; visual integration corrected in Task 4 | Commit `efa726d`; the Task 4 integration now restores the confirmed Codex-like sidebar composition, selected project tree, account footer, and current-project new-conversation link |
| 08 / Task 3 | Inputs, Knowledge, Templates, Outputs pages and upload conflicts | Complete | Commit `05d9043`; Linux backend 57 passed, OpenAPI 11 passed, frontend 193 passed, lint/build/check:api passed, independent review PASS |
| 08 / Task 4 | Project-bound conversations and browser-computer attachments | Complete | Confirmed-reference UI parity correction implemented and visually inspected: no top action strip or page-local conversation column; empty-state composer, browser upload, current-project context, sidebar project tree, separate Outputs, and overflow-held session controls are present. Three independent review passes found and drove fixes for project activation, requested-session/StrictMode lifecycle, upload/send and stale-retry races, focus visibility, and back-navigation reactivation; final independent review PASS. Gates: backend 121 passed, OpenAPI 11 passed, frontend 206 passed, conversations E2E 2 passed, lint/build/check:api passed |
| 08 / Task 5 | Outputs, Runtime, and Logs pages | Complete | Outputs is preview/download/delete only; Runtime is a read-only sanitized snapshot with serialized project activation and project-identity validation; Logs is a bounded project-scoped sanitized event projection with JSON download and no raw filesystem/log API. Gates: Linux backend 358 passed, frontend 214 passed, targeted E2E 3 passed, Ruff/lint/build/check:api passed, independent review PASS |
| 09 / Task 1 | Global knowledge file service and API | Complete | Commit `0d6f8b1`; fixed hidden data-root storage, logical safe-file API, session authentication and control-lease mutations. Gates: service/API/OpenAPI 21 passed, existing project-file regression 25 passed, targeted Ruff passed |
| 09 / Task 2 | Composite retrieval with project priority | Complete | Commit `554bdd5`; namespaced safe opening, project-first path/content de-duplication, deterministic ranking, tool namespace output and ledger metadata. Gates: focused composite/research/source-ledger/knowledge-context 23 passed, targeted Ruff passed |
| 09 / Task 3 | Run-stable knowledge provenance | Implementation complete; commit pending | Source list freeze, logical namespaced SHA-256 manifest, Agent/workflow use and optional Runtime global-root threading are implemented. Gates: required reporting/application regression 129 passed, lifecycle/manager/global-knowledge/golden supplement 38 passed, targeted Ruff passed |
| 09 / Task 4 | React global knowledge page | Pending | Start only after the Task 3 commit is recorded below |
| 10 | Simple model settings | Pending | Not implemented in this redesign cycle |
| 11 | Integration, Podman deployment, release acceptance | Pending | Not implemented in this redesign cycle |

**Current handoff rule:** Plan 08 and Plan 09 Tasks 1-2 are complete. Task 3 implementation and gates are complete but its commit must be recorded before Task 4 begins. Do not claim the full Phase 1 redesign complete while Plans 09-11 remain pending. Preserve the untracked `.venv-wsl/` directory and do not stage it. The user-confirmed screenshot, not the discarded page-local conversation-list layout, is the visual baseline.

## Current facts

- React and Electron use the versioned FastAPI boundary; the existing PyQt/application runtime remains supported.
- The Linux Compose stack builds and becomes healthy with one Gunicorn worker and one authoritative Runtime; FastAPI is internal on `9000`, while Nginx is the only host-published service on `9090`.
- The public deployment verifier passes all 8 checks against the real local Linux-container stack.
- The full repository suite passes: 1,957 passed and 14 skipped; current release/deploy subset: 36 passed.
- Legacy-vs-service runtime and old-workspace Reporting golden tests pass.
- Python, frontend, and desktop dependency audits report zero known vulnerabilities after lock updates.
- Gitleaks 8.30.1 passes all 172 commits after ten exact, reviewed test-fixture fingerprints were documented in `.gitleaksignore`.
- A Trivy image scan is still required on the target Linux host because Docker Hub returned EOF while fetching Trivy; Docker Scout required an authenticated Docker ID.
- New Phase 1 code passes Ruff. The repository-wide Ruff command still reports 127 pre-existing legacy GUI/Reporting findings; resolving those would be a separate core cleanup cycle, not a Phase 1 deployment change.
- The parity matrix is machine-valid at `tested`; legacy foundation rows already accepted remain accepted. Remaining rows require an actual reviewer before the release-mode matrix check can pass.

## Meaning of status

- `Complete in repository`: code, tests, scripts, and operator documentation exist.
- `Automated gate passes`: the named command ran successfully in this cycle.
- `External`: requires the target Linux host, organization TLS/network policy, credentials, or a human product reviewer.
