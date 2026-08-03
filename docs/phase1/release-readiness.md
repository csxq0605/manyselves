# Phase 1 release readiness

Decision date: 2026-08-03. Candidate implementation commit: `02bb2a2` on `feature/react-fastapi-manyselves`.

## Outcome

The Phase 1 repository implementation is complete and deployable for a controlled Linux pilot. Automated browser, Electron, API, persistence, recovery, dependency, secret, Compose, backup/restore, and real-stack deployment checks have evidence.

Final automated results include 1,957 repository tests passed / 14 skipped, 36 release/deploy tests passed, 140 React tests, 11 browser flows, 5 Electron unit tests, 2 Electron application flows, deterministic OpenAPI, protected-core freeze, a 113-row machine-valid matrix, and an 8/8 live deployment verification. The reviewed direct-LAN layout was rebuilt and reverified with FastAPI internal on `9000` and Nginx on host port `9090`.

Production release decision: **NO-GO pending external acceptance**.

This is not a code-completeness failure. The remaining release actions require external state and must not be fabricated:

1. Run and pass an image CVE scan on the target Linux host (Trivy could not be pulled in this environment).
2. Complete the TLS-backed clean Linux upgrade/rollback/restore pilot.
3. Conduct row-by-row product reviewer acceptance so every matrix row is `accepted` or explicitly approved `waived`.

The repository-wide Ruff command also reports 127 legacy GUI/Reporting findings. New Phase 1 code passes Ruff; this known baseline is not silently labeled green and is deferred because correcting duplicate legacy renderer definitions and naming conventions is outside the frozen Phase 1 architecture.

## Candidate artifacts

- API image: `sha256:5c1b01ac8dc81ca37747863434499c65c369764a55d9d3322f2f394a037124f0`
- Web image: `sha256:4b3ee00271500f3a8dcd589eedd1929c2d384e7dba144afe038546a416a82d24`
- Backup drill digest: `CE7E0D61280F1477123D5DB3248DF7D99F22E020B365389157A2B7178E548B7B`

## Rollback

Keep prior immutable image digests and a verified pre-upgrade backup. Stop the stack, restore the prior image variables, restore data only if the upgrade changed authoritative files, run `up -d --wait`, then execute `scripts/verify_deployment.py`. Phase 1 introduces no one-way workspace migration.
