# Manyselves Phase 1 Verification and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove functional completeness, persistence compatibility, recovery, deployment safety, and release readiness before Phase 1 is declared complete.

**Architecture:** A machine-checked parity matrix links every legacy capability to API/bridge implementation and automated evidence. Golden fixtures compare PyQt/application behavior with REST/SSE behavior, while deployment and recovery drills verify the packaged system on a clean Linux host.

**Tech Stack:** pytest, Playwright, Vitest, Docker Compose, fixture workspaces, fake provider, SHA-256 manifests, CSV verification.

## Global Constraints

- No release based on manual confidence alone; every matrix row has executable evidence.
- Existing persisted workspaces are tested without one-way migration.
- Golden comparisons normalize timestamps/IDs only; they do not ignore semantic fields or artifact content.
- Real-provider tests are smoke tests and never replace deterministic fake-provider coverage.
- PyQt remains installed and supported as fallback after Phase 1 release.

---

### Task 1: Complete Evidence and Machine-Validate the Existing Feature Parity Inventory

**Files:**
- Modify: `docs/phase1/feature-parity.csv`
- Create: `docs/phase1/parity-rules.yaml`
- Create: `scripts/verify_phase1_matrix.py`
- Create: `tests/release/test_feature_matrix.py`

**Interfaces:**
- Consumes: all legacy GUI widgets/routes/tests and all new browser/Electron/API evidence.
- Produces: deterministic matrix validator used by Gate E.

- [ ] **Step 1: Write matrix schema and evidence-existence tests**

```python
def test_every_matrix_row_has_unique_id_and_legacy_evidence(matrix):
    assert len({row.id for row in matrix}) == len(matrix)
    assert all(row.legacy_evidence for row in matrix)


def test_accepted_rows_reference_existing_tests(matrix, repo_root):
    for row in matrix:
        if row.status == "accepted":
            for evidence in row.required_evidence:
                assert evidence_file(evidence, repo_root).exists(), evidence
```

- [ ] **Step 2: Run the validator tests and expose incomplete rows**

Run: `uv run pytest tests/release/test_feature_matrix.py -q`
Expected: FAIL while rows, required evidence, or status values are incomplete.

- [ ] **Step 3: Audit the pre-implementation inventory and complete its evidence**

Compare the matrix created before implementation against current GUI modules and Git history; missing legacy rows fail the audit and must be added with an explanation in the review diff. Use ID families `PROJECT`, `FILE`, `EDITOR`, `PREVIEW`, `PYTHON`, `CONV`, `MESSAGE`, `AGENT`, `TOOL`, `TASK`, `CONFIG`, `REPORT`, `DESKTOP`, `RECOVERY`, and `DEPLOY`. `parity-rules.yaml` declares which families require browser, Electron, API, legacy, or manual evidence. Validator permits statuses only `planned`, `implemented`, `tested`, `accepted`, `waived`; `waived` requires a non-empty reviewer and rationale field.

- [ ] **Step 4: Run matrix verification**

Run:

```powershell
uv run pytest tests/release/test_feature_matrix.py -q
uv run python scripts/verify_phase1_matrix.py docs/phase1/feature-parity.csv --allow-status tested
```

Expected: PASS for schema/evidence integrity; release mode still fails until all rows are accepted.

- [ ] **Step 5: Commit the complete inventory**

```powershell
git add docs/phase1 scripts/verify_phase1_matrix.py tests/release/test_feature_matrix.py
git diff --cached --check
git commit -m "test: make phase one parity machine verifiable"
```

### Task 2: Add Legacy-vs-Service Golden Runtime and Artifact Comparisons

**Files:**
- Create: `tests/release/fixtures/workspace_v1/`
- Create: `tests/release/fake_provider.py`
- Create: `tests/release/runtime_harness.py`
- Create: `tests/release/test_runtime_golden.py`
- Create: `tests/release/test_reporting_golden.py`
- Create: `tests/release/fixtures/expected/`

**Interfaces:**
- Consumes: PyQt/application adapter, FastAPI test app, fake provider, old-format fixture workspace.
- Produces: normalized event/artifact manifests proving equivalent behavior.

- [ ] **Step 1: Write golden comparison tests**

```python
@pytest.mark.asyncio
async def test_message_tool_checkpoint_flow_matches_legacy_and_service(golden_workspace):
    legacy = await run_legacy_flow(golden_workspace.copy("legacy"))
    service = await run_service_flow(golden_workspace.copy("service"))
    assert normalize_events(service.events) == normalize_events(legacy.events)
    assert service.conversation_records == legacy.conversation_records
    assert service.workspace_manifest == legacy.workspace_manifest


@pytest.mark.asyncio
async def test_reporting_resume_produces_equivalent_verified_outputs(golden_workspace):
    legacy = await run_legacy_reporting_resume(golden_workspace.copy("legacy-report"))
    service = await run_service_reporting_resume(golden_workspace.copy("service-report"))
    assert service.report_state == legacy.report_state
    assert service.output_verification == legacy.output_verification
    assert service.semantic_artifact_manifest == legacy.semantic_artifact_manifest
```

- [ ] **Step 2: Run golden tests and inspect semantic mismatches**

Run: `uv run pytest tests/release/test_runtime_golden.py tests/release/test_reporting_golden.py -q`
Expected: tests reveal any adapter-induced ordering, persistence, rollback, or artifact difference.

- [ ] **Step 3: Normalize only nondeterministic fields**

Normalization may replace generated UUIDs with encounter-order aliases, normalize path separators, and zero timestamps. It must retain event type/order, agent/session relationships, message/tool contents, task state, checkpoint files, reporting state, output verification, document text/table/image order, and file hashes for deterministic artifacts.

Fix differences in adapters or DTO mapping; do not change protected core behavior or broaden normalization to hide them.

- [ ] **Step 4: Run golden and existing regression suites**

Run:

```powershell
uv run pytest tests/release/test_runtime_golden.py tests/release/test_reporting_golden.py -q
uv run pytest tests/test_conversations.py tests/test_checkpoints.py tests/reporting -q
uv run python scripts/check_phase1_core_freeze.py --base-file docs/phase1/core-freeze-base.txt
```

Expected: PASS and no protected changes.

- [ ] **Step 5: Commit golden evidence**

```powershell
git add tests/release
git diff --cached --check
git commit -m "test: compare service behavior with legacy runtime"
```

### Task 3: Add Failure, Recovery, Concurrency, and Security Test Matrix

**Files:**
- Create: `tests/release/test_failure_recovery.py`
- Create: `tests/release/test_concurrent_clients.py`
- Create: `tests/release/test_security_boundaries.py`
- Create: `frontend/e2e/failure-recovery.spec.ts`
- Create: `desktop/e2e/security.spec.ts`
- Create: `docs/phase1/security-review.md`

**Interfaces:**
- Consumes: packaged API behavior, SSE, control lease, filesystem services, React and Electron clients.
- Produces: deterministic evidence for every required fault and trust-boundary case.

- [ ] **Step 1: Write parameterized server recovery cases**

```python
@pytest.mark.parametrize("fault", [
    "provider_timeout", "provider_429", "disk_full", "upload_disconnect", "runtime_shutdown",
    "stale_revision", "sse_cursor_evicted", "report_waiting_user", "invalid_checkpoint",
])
@pytest.mark.asyncio
async def test_fault_has_stable_error_and_recovery(fault, recovery_harness):
    result = await recovery_harness.inject_and_recover(fault)
    assert result.error_code
    assert result.user_recovery_action
    assert result.runtime_consistent
    assert result.no_partial_authoritative_file
```

- [ ] **Step 2: Write concurrent-client and Electron boundary cases**

```python
@pytest.mark.asyncio
async def test_only_lease_holder_mutates_runtime(two_clients):
    holder, observer = two_clients
    assert (await holder.acquire_lease()).status_code == 201
    assert (await observer.send_message("blocked")).status_code == 423
    assert (await observer.runtime_snapshot()).status_code == 200
```

Electron E2E must attempt unexpected navigation, arbitrary IPC channel use, untrusted downloaded-path open, local path disclosure, and token read from renderer; every attempt must fail without crashing the application.

- [ ] **Step 3: Run fault suites and fix only adapter/client/deployment defects**

Run:

```powershell
uv run pytest tests/release/test_failure_recovery.py tests/release/test_concurrent_clients.py tests/release/test_security_boundaries.py -q
npm --prefix frontend run e2e -- failure-recovery.spec.ts
npm --prefix desktop run e2e -- security.spec.ts
```

Expected: PASS; no fault leaves corrupt authoritative files, duplicate terminal messages, unreleased leases, orphan SSE clients, exposed secrets, or an unrecoverable UI.

- [ ] **Step 4: Run exact dependency, image, secret, and Electron security checks**

Run:

```powershell
uvx pip-audit
npm --prefix frontend audit --audit-level=high
npm --prefix desktop audit --audit-level=high
gitleaks version
gitleaks detect --source . --redact --no-banner --exit-code 1
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy image --severity HIGH,CRITICAL --exit-code 1 manyselves-api:phase1
```

Record exact tool versions, commands, exit codes, and findings in `docs/phase1/security-review.md`. Any unresolved high or critical dependency, image, secret, Electron, path-traversal, or credential-exposure finding blocks release.

- [ ] **Step 5: Commit recovery and security evidence**

```powershell
git add tests/release frontend/e2e/failure-recovery.spec.ts desktop/e2e/security.spec.ts docs/phase1/security-review.md
git diff --cached --check
git commit -m "test: verify recovery and security boundaries"
```

### Task 4: Perform Clean Linux Deployment, Upgrade, Backup, and Restore Drill

**Files:**
- Create: `docs/phase1/linux-pilot-checklist.md`
- Create: `docs/phase1/backup-restore-evidence.md`
- Create: `scripts/verify_deployment.py`
- Create: `tests/release/test_deployment_verifier.py`

**Interfaces:**
- Consumes: versioned images, Compose file, backup/restore scripts, fixture workspace.
- Produces: repeatable deployment verifier and signed pilot evidence.

- [ ] **Step 1: Write verifier contract tests**

```python
def test_deployment_verifier_checks_runtime_and_persistence(fake_deployment):
    result = verify_deployment(fake_deployment.url, fake_deployment.token)
    assert result.checks == {
        "live": True, "ready": True, "single_runtime": True, "project": True,
        "conversation": True, "file_round_trip": True, "sse": True, "artifact_download": True,
    }
```

- [ ] **Step 2: Run verifier tests and verify failure**

Run: `uv run pytest tests/release/test_deployment_verifier.py -q`
Expected: FAIL because deployment verifier does not exist.

- [ ] **Step 3: Implement and execute the pilot checklist**

On a clean supported Linux host: install Docker/Compose, create a non-root service account, configure TLS and data directory permissions, start Compose, import the old fixture workspace, run browser and Electron smoke flows, execute one deterministic Agent flow, create and resume one Reporting flow, download verified output, restart containers, and confirm state persistence.

Then back up, stop services, restore into a new empty data directory, start services against restored data, and rerun the deployment verifier. Perform one image upgrade and rollback while preserving the same volume. Record commands, image digests, host distribution, timestamps, checksums, and reviewer identity in the evidence documents.

- [ ] **Step 4: Confirm pilot evidence and matrix rows**

Run:

```powershell
uv run python scripts/verify_deployment.py --url https://pilot.example.internal --token-env MANYSELVES_ACCESS_TOKEN
uv run python scripts/verify_phase1_matrix.py docs/phase1/feature-parity.csv --allow-status tested
```

Expected: deployment verifier PASS; matrix is structurally complete and awaiting only final reviewer acceptance.

- [ ] **Step 5: Commit reproducible drill assets and evidence**

```powershell
git add docs/phase1/linux-pilot-checklist.md docs/phase1/backup-restore-evidence.md scripts/verify_deployment.py tests/release/test_deployment_verifier.py docs/phase1/feature-parity.csv
git diff --cached --check
git commit -m "test: record clean host deployment drill"
```

### Task 5: Execute Gate E and Produce Release Decision

**Files:**
- Create: `docs/phase1/release-readiness.md`
- Create: `docs/phase1/known-limitations.md`
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `docs/phase1/feature-parity.csv`

**Interfaces:**
- Consumes: all prior gates and reviewer decisions.
- Produces: accepted 100% matrix, release readiness record, explicit limitations, user-facing entry points.

- [ ] **Step 1: Run the complete backend and protected-path gate**

Run:

```powershell
uv run pytest -q
uv run ruff check manyselves tests scripts
uv run python scripts/export_openapi.py --check frontend-contract/openapi.json
uv run python scripts/check_phase1_core_freeze.py --base-file docs/phase1/core-freeze-base.txt
```

Expected: PASS and zero protected changes.

- [ ] **Step 2: Run complete browser, desktop, and deployment gates**

Run:

```powershell
npm --prefix frontend run verify
npm --prefix desktop run verify
uv run pytest tests/deploy tests/release -q
docker compose -f deploy/compose.yaml --env-file deploy/env.example config
```

Expected: PASS with archived test reports, Playwright traces for failures only, package checksums, and image digests.

- [ ] **Step 3: Conduct row-by-row reviewer acceptance**

For each matrix row, open the legacy evidence, run the named API/browser/Electron test, inspect the result, set `status=accepted`, and record reviewer. Do not bulk-change statuses. Any intentional difference requires `waived`, rationale, and product-owner approval; release mode still requires every row to be either `accepted` or explicitly approved `waived` according to `parity-rules.yaml`.

- [ ] **Step 4: Write the release decision and limitations**

`release-readiness.md` records exact commit, artifacts, images, gates, matrix counts, backup/restore drill, security result, rollback procedure, and go/no-go decision. `known-limitations.md` states: one Runtime per stack, one active project, one controller lease, deployment-token rather than user RBAC, trusted-network-only execution, server-authoritative files, no local sync, and one stack per concurrently running scenario.

Update README files with browser, Electron, PyQt, development, and Compose entry points without calling Phase 1 multi-tenant.

- [ ] **Step 5: Run final matrix check and commit release record**

Run:

```powershell
uv run python scripts/verify_phase1_matrix.py docs/phase1/feature-parity.csv
git diff --check
```

Expected: PASS with 100% accepted or explicitly approved rows.

```powershell
git add docs/phase1 README.md README_zh.md
git diff --cached --check
git commit -m "docs: approve phase one release readiness"
```
