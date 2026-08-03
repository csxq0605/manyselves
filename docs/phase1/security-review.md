# Phase 1 security review

Review date: 2026-08-03. Scope: Python lock, React/Electron locks, Git history, Electron trust boundary, filesystem boundary, Compose images.

## Executed checks

| Check | Version / command | Result |
|---|---|---|
| Python dependencies | `pip-audit 2.10.1`; `uv export --format requirements-txt --no-hashes --no-emit-project \| uvx pip-audit -r /dev/stdin` | PASS: no known vulnerabilities after upgrading idna, Pillow, pydantic-settings, pytest, and pytest-asyncio |
| React dependencies | npm 10.9.3; `npm audit --audit-level=high --registry=https://registry.npmjs.org` | PASS: 0 vulnerabilities. The unused React Router wrapper was removed because no release available at review time avoided both High advisory ranges. |
| Electron dependencies | npm 10.9.3; same audit command | PASS: 0 vulnerabilities |
| Secret history | Gitleaks 8.30.1; `detect --source /repo --redact --no-banner --exit-code 1` | PASS: 172 commits, no unreviewed leaks. Ten exact historical false-positive fingerprints are documented in `.gitleaksignore`; no path-wide rule is used. |
| Release security tests | `pytest tests/release/test_failure_recovery.py tests/release/test_concurrent_clients.py tests/release/test_security_boundaries.py -q` | PASS: 21 tests |
| Browser recovery | `npm --prefix frontend run e2e -- failure-recovery.spec.ts` | PASS: 1 flow |
| Electron boundary | `npm --prefix desktop run e2e -- security.spec.ts` | PASS: 1 flow |

Electron E2E verifies no Node/require access, denied unexpected navigation/window creation, denied arbitrary downloaded-path open, and no bridge in an untrusted renderer. Packaged file renderers are now matched to the exact packaged `index.html`, not any file URL ending in that name.

## Image evidence and remaining production check

- API image: `sha256:83bceffd5c84963eeb827c5dc1c7b23fecbdd65341a84a053fcad03f713cc6c6`
- Web image: `sha256:aad90bc46261f0c5da7c7118b241a298b681f95d248d334424099a388bebef02`
- Trivy did not execute: two pulls of `aquasec/trivy:latest` failed at Docker Hub with `EOF`.
- Docker Scout 1.18.3 was installed but required Docker-ID login.

Therefore the repository is ready for deployment testing, but production release remains blocked until the target Linux operator runs the Trivy command in `linux-pilot-checklist.md` and records zero unresolved fixable High/Critical findings.
