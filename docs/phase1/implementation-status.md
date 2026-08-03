# Phase 1 implementation status

Updated: 2026-08-03. Branch: `feature/react-fastapi-manyselves`.

This file separates implemented code, automated evidence, and external production acceptance. “Implemented” never means a reviewer or production pilot was fabricated.

| Plan | Scope | Implementation | Automated gate |
|---|---|---|---|
| 01 | Runtime foundation and protected boundary | Complete | Gate A evidence complete; protected-core freeze passes |
| 02 | FastAPI, SSE, control lease, stable API | Complete | Gate B accepted and regression-covered |
| 03 | React project/file/editor/preview workspace | Complete | Gate C browser/unit/build evidence passes |
| 04 | React conversations, agents, reporting, settings | Complete | Gate C full parity evidence passes |
| 05 | Secure Electron client, Compose, backup/restore | Complete | Gate D package/E2E/deployment evidence passes |
| 06 | Compatibility, recovery, security, deployment verifier, release record | Complete in repository | Gate E automated checks pass except the external image scanner could not be downloaded; clean-host pilot and row-by-row product acceptance remain external |

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
