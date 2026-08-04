# Phase 1 Light Web Verification Evidence

Date: 2026-08-04

Branch: `codex/phase1-light-web`

Current release commit: final handoff commit containing this file. Run `git rev-parse HEAD` in `codex/phase1-light-web` for the immutable hash.

## Local Verification Completed

API contract:

```text
WSL: python -m pytest tests/webapi/test_openapi_contract.py -q
13 passed
```

Task 2 browser journeys:

```text
npm.cmd run e2e -- --workers=1 e2e/auth.spec.ts e2e/project-navigation.spec.ts e2e/global-knowledge.spec.ts e2e/conversations.spec.ts
5 passed
```

Upload component regression:

```text
npm.cmd test -- --run src/features/knowledge/GlobalKnowledgePage.test.tsx src/features/files/ProjectDirectoryPage.test.tsx --maxWorkers=1
2 files passed, 18 tests passed
```

Release compatibility/security/concurrency/recovery:

```text
WSL: python -m pytest tests/release/test_phase1_light_web_compatibility.py tests/release/test_security_boundaries.py tests/release/test_concurrent_clients.py tests/release/test_failure_recovery.py -q
26 passed
```

Deployment verifier:

```text
WSL: python -m pytest tests/release/test_deployment_verifier.py -q
9 passed
```

Full frontend verification:

```text
npm.cmd run verify
check:api passed
eslint passed
Vitest: 54 files passed, 229 tests passed
Vite build passed
Playwright: 14 passed
```

Full Python verification:

```text
WSL: python -m pytest -q
2041 passed, 17 skipped in 288.87s
```

Ruff status:

```text
WSL: ruff check tests/release/test_phase1_light_web_compatibility.py
All checks passed
```

```text
WSL: ruff check scripts/verify_deployment.py tests/release/test_phase1_light_web_compatibility.py tests/release/test_deployment_verifier.py
All checks passed
```

```text
WSL: ruff check manyselves tests scripts deploy
126 existing legacy findings remain. They are not introduced by Plan 11; the touched/new Python files above are Ruff-clean.
```

## Server Verification Still Required

This workstation does not have Podman installed, so image build and live smoke-stack evidence must be collected on the Linux server.

Run from the release source root on the server:

```bash
podman build -f deploy/api/Dockerfile -t localhost/manyselves-api:phase1 .
podman build -f deploy/web/Dockerfile -t localhost/manyselves-web:phase1 .
podman compose -p manyselves-phase1-smoke -f deploy/compose.yaml --env-file deploy/smoke.env.example config
podman compose -p manyselves-phase1-smoke -f deploy/compose.yaml --env-file deploy/smoke.env.example up -d --no-build
podman ps --filter name=manyselves-phase1-smoke
uv run python scripts/verify_deployment.py --url http://127.0.0.1:19090 --username admin --password yuanxi@2026
podman compose -p manyselves-phase1-smoke -f deploy/compose.yaml --env-file deploy/smoke.env.example down
```

Expected:

- API image: `localhost/manyselves-api:phase1`
- Web image: `localhost/manyselves-web:phase1`
- Smoke web bind: `127.0.0.1:19090`
- Normal LAN web bind: `192.168.8.28:9090`
- API port: `9000` inside the Compose network only
- No Access Token, Redis, MySQL, Milvus, MinIO, RBAC, multi-worker Runtime, or domain requirement in Phase 1

Record `podman image inspect localhost/manyselves-api:phase1` and `podman image inspect localhost/manyselves-web:phase1` image IDs after the server build.

## Phase 1 Limits

- Shared administrator login only: default `admin / yuanxi@2026`.
- One authoritative Runtime per stack; do not scale API replicas or Gunicorn workers.
- Browser uploads come from the operator's browser computer and are stored in the server data directory.
- Global knowledge is shared across projects; project knowledge remains scoped to a project.
- Templates and Outputs are separate: templates are user-managed inputs to generation, outputs are generated final artifacts.
- HTTP-only LAN deployment has no TLS. Keep it on a trusted network or add external TLS before wider exposure.
