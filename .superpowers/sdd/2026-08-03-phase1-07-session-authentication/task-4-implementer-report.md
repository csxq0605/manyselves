# Task 4 implementer report

## Result

- Commit: `cd2493d deploy: use administrator login for operations`
- Removed the legacy deployment access-token setting and active deployment references.
- Added server-only administrator environment variables and cookie-session operations for verification and backup clients.
- Preserved API port 9000, Nginx port 9090, IP-only LAN origin, one API replica, and the existing Runtime architecture.
- Restored complete Docker/Podman source-build, prebuilt-image, health, operation, backup, restore, and rollback instructions.

## Fresh verification

- Focused backend/deploy: 31 passed.
- Frontend auth/transport: 28 passed.
- Frontend build: passed.
- Frontend lint: passed.
- PowerShell backup parser: passed.
- OpenAPI generated artifact check: passed.
- `verify_deployment.py --help`: username/password options present; no token option.
- `git diff --cached --check`: passed before commit.

## Environment qualification

Windows cannot collect the Linux-only Reporting lock implementation without an `fcntl` import shim. With a collection-only shim, the full WebAPI run reached an existing Windows incompatibility in `ConversationService.flush`: `os.fsync()` on a read-only descriptor returned `OSError: [Errno 9]`. The focused authentication/deployment set does not exercise POSIX locking and passed with the shim. WSL was unavailable during this task with `Wsl/Service/CreateInstance/E_ACCESSDENIED`; the final Linux/container regression remains a Plan 11 release gate.

## Scope check

The only production Python module changed from the Task 3 baseline is `manyselves/webapi/settings.py`, removing the obsolete `access_token` field. No Agent, Loop, Reporting, model protocol, or Runtime orchestration module changed.

## Fix round 1: independent review follow-up (2026-08-04)

### Scope and commit

- Follow-up commit: `fix: harden session-authenticated deployment operations`.
- Changed only `deploy/backup/backup.sh`, `docs/deployment/linux-compose.md`, `scripts/verify_deployment.py`, and `tests/release/test_deployment_verifier.py`.
- No Agent, Loop, Reporting, model protocol, or Runtime orchestration modules changed; no Plan 08 work was entered.

### Root causes and fixes

- The Linux deployment guide described a dedicated non-root account but had it create `/opt` and `/srv` paths itself. Both source-build and prebuilt-image paths now use `sudo install -d` with explicit `manyselves:manyselves` ownership before using `sudo -u manyselves -H sh -c` for extraction, image loading, and Compose commands.
- `backup.sh` interpolated administrator credentials directly into a curl `-d` argument. Its JSON encoder now reads temporary process environment variables, uses Python `json.dumps`, and pipes JSON through stdin to `curl --data-binary @-`; credentials do not appear in curl arguments. Maintenance and lease cleanup bodies use the same encoding path.
- The verifier regression suite now proves login cookie reuse with a real `httpx.Client` cookie jar; protected checks do not use Authorization; temporary-file, lease, and logout cleanup occur in that order after a failed check; and the verifier does not call logout after an unsuccessful login. The verifier now records successful login before entering the logout cleanup path.
- Tests also preserve the shell and PowerShell login → lease → maintenance and maintenance-release → lease-release → logout contracts, and exercise credentials containing quotes, backslashes, and newlines.

### TDD evidence

- RED: `uv run pytest tests/release/test_deployment_verifier.py -q` initially failed for the missing stdin JSON transport and the missing root-to-service-account Linux instructions (2 failed, 4 passed after correcting one test’s source-order assertion).
- RED: the new failed-login cleanup test failed as expected: a 401 login attempt was followed by an unwanted `/api/v1/auth/logout` request.
- GREEN: after the minimal changes, `uv run pytest tests/release/test_deployment_verifier.py tests/deploy/test_compose_contract.py -q` passed with `11 passed`.

### Fresh verification

- `uv run pytest tests/release/test_deployment_verifier.py tests/deploy/test_compose_contract.py -q`: 11 passed.
- `uv run python scripts/verify_deployment.py --help`: passed; only username/password options are present.
- PowerShell parser for `deploy/backup/backup.ps1`: passed.
- `git diff --check`: passed.

### Environment qualification / concerns

- This Windows host has no `sh`, so `sh -n deploy/backup/backup.sh` could not be run locally. The POSIX script’s behavior is covered by Python-level JSON round-trip and command-contract tests.
- WSL remains unavailable in this environment; retain the Linux/container validation as the existing release gate.

## Fix round 2: cleanup execution and non-root engine setup (2026-08-04)

### Scope

- Follow-up commit: fix: verify backup cleanup and rootless engine setup.
- Changed the Linux deployment guide and deployment/release tests only. The backup production scripts already continued cleanup correctly; this round adds execution-level regression coverage rather than changing their behavior.
- No Agent, Loop, Reporting, Runtime orchestration, or Plan 08 files changed.

### TDD and verification

- RED: the new engine-context documentation tests failed because the guide had no mutually exclusive Docker/rootless Docker/rootless Podman setup or service-session instructions.
- RED harness investigation: a Git Bash PATH fake did not execute because Windows temporary files have no POSIX executable bit. The harness was changed to source Bash functions through BASH_ENV, which works on both Git Bash and Linux CI; this is a test-harness correction, not a product change.
- GREEN: uv run pytest tests/deploy/test_backup_client_cleanup.py tests/deploy/test_compose_contract.py tests/release/test_deployment_verifier.py -q passed with 14 tests.
- Git Bash shell syntax, PowerShell backup parser, and git diff --check passed.

### Added execution evidence

- The POSIX test runs backup.sh in Git Bash with fake curl and rm functions. It forces maintenance release to fail, then proves lease release, logout, and cookie-file deletion still occur in order.
- The PowerShell test runs backup.ps1 in an isolated PowerShell process with mocked HTTP commands. It forces both archive failure and maintenance release failure, then proves its finally path still releases the lease and logs out.
- The Linux guide now makes an explicit one-engine choice: Docker group (with its root-equivalent privilege stated), rootless Docker, or rootless Podman. Rootless Podman setup includes linger, a service-user login session, XDG_RUNTIME_DIR, and podman info before the extraction and Compose steps.

### Environment qualification

- WSL remains unavailable with E_ACCESSDENIED, but Git for Windows Bash is available and was used for the real POSIX cleanup test and shell syntax check.
- The PowerShell test provides a local replacement for WebRequestSession because this Windows PowerShell environment does not expose that type until web cmdlets are loaded; the backup script itself is unchanged and the mocked client calls execute in a separate process.

## Fix round 3: rootless Docker runtime ordering (2026-08-04)

- RED: the deployment contract failed because rootless Docker installed before its XDG runtime directory and user-manager checks, and masked context creation with || true.
- GREEN: the rootless Docker instructions now enable linger, enter the service account session, export and test XDG_RUNTIME_DIR, verify systemctl --user is-active default.target, then install the daemon.
- Docker context creation now fails visibly instead of being masked.
- Verification: uv run pytest tests/deploy/test_compose_contract.py tests/deploy/test_backup_client_cleanup.py -q passed with 6 tests; git diff --check passed.
- `uv run ruff` could not run because Ruff is not installed in the resolved environment (`program not found`); no dependency changes were made to expand scope.
