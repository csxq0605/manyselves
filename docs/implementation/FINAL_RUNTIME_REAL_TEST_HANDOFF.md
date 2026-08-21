# Final Declarative Runtime Real-Test Handoff

## Outcome and scope

WP-00 through WP-12 and all four migration stages are implemented. This is the
single deferred real-test handoff for the completed program. It covers a real
Provider, a real project, the browser UI, the FastAPI server, the neutral second
Capability, Legacy/declarative equivalence, persisted recovery, delivery
artifacts, and cost projection.

The implementation deliberately leaves the existing Reporting API on the
Legacy runner. The generic `distribution-reporting` Workflow is the explicit
declarative entry. A passing test does **not** switch the default or remove the
Legacy runner; that remains a separate release decision.

No full regression is part of this handoff.

## Tested revision

- Repository: `csxq0605/manyselves`
- Base: `agent/declarative-runtime-plan`
- Branch: `agent/declarative-runtime-implementation`
- Draft PR: <https://github.com/csxq0605/manyselves/pull/3>
- Last functional commit before this handoff: `0060fd6`
- Required Python: 3.12+
- Required Node.js: 22+
- Required command-line tools for the API steps: `curl`, `jq`, and `uuidgen`
- Recommended browser: current Chrome or Edge
- Server process count: one worker

Before testing, from the repository root run:

```bash
git status -sb
git branch --show-current
git log -1 --oneline
uv sync --frozen
cd frontend
npm ci
npm run build
cd ..
```

Expected branch: `agent/declarative-runtime-implementation`. The worktree must
have no unexplained changes. Do not run the test against `main` or against the
plan branch.

## Prepare two isolated copies of one real project

Choose a real source project that contains the exact `Inputs/`, `Knowledge/`,
and optional `Templates/` directories to be tested. Do not copy an earlier
`Work/` or `Outputs/` directory into either test workspace.

Set these paths to new, persistent directories outside the repository:

```bash
export MS_REAL_PROJECT="/absolute/path/to/the/real-project"
export MS_FINAL_TEST_ROOT="/absolute/path/to/manyselves-final-runtime-test"
export MS_LEGACY_DATA="$MS_FINAL_TEST_ROOT/legacy-data"
export MS_DECL_DATA="$MS_FINAL_TEST_ROOT/declarative-data"
export MS_PROJECT_ID="final-runtime-test"
```

Create the two test workspaces:

```bash
mkdir -p "$MS_LEGACY_DATA/$MS_PROJECT_ID" "$MS_DECL_DATA/$MS_PROJECT_ID"
cp -R "$MS_REAL_PROJECT/Inputs" "$MS_LEGACY_DATA/$MS_PROJECT_ID/Inputs"
cp -R "$MS_REAL_PROJECT/Inputs" "$MS_DECL_DATA/$MS_PROJECT_ID/Inputs"
cp -R "$MS_REAL_PROJECT/Knowledge" "$MS_LEGACY_DATA/$MS_PROJECT_ID/Knowledge"
cp -R "$MS_REAL_PROJECT/Knowledge" "$MS_DECL_DATA/$MS_PROJECT_ID/Knowledge"
if test -d "$MS_REAL_PROJECT/Templates"; then
  cp -R "$MS_REAL_PROJECT/Templates" "$MS_LEGACY_DATA/$MS_PROJECT_ID/Templates"
  cp -R "$MS_REAL_PROJECT/Templates" "$MS_DECL_DATA/$MS_PROJECT_ID/Templates"
fi
```

Use only newly created `legacy-data` and `declarative-data` directories. If
either already contains a run, choose another `MS_FINAL_TEST_ROOT`; do not
delete or overwrite prior evidence.

Use the same Provider, model, model parameters, project inputs, instruction,
missing-evidence policy, and budgets in the paired runs. Run them sequentially.

## A. Neutral Capability in the browser

Start the declarative test server in terminal A:

```bash
MANYSELVES_DATA_ROOT="$MS_DECL_DATA" \
MANYSELVES_INITIAL_PROJECT_ID="$MS_PROJECT_ID" \
MANYSELVES_ADMIN_USERNAME=admin \
MANYSELVES_ADMIN_PASSWORD='replace-with-a-test-password' \
uv run python run_web.py --host 127.0.0.1 --port 9092 --data-dir "$MS_DECL_DATA"
```

In the browser:

1. Open <http://127.0.0.1:9092/> and log in as `admin` with the password used
   above.
2. Open the account menu, choose **模型设置**, select the real Provider/model,
   enter the API Key, save it, and run **测试连接**. The connection must pass.
3. Open project `final-runtime-test`, then choose **工作流** in the sidebar.
4. Select Workflow `parameter-adjustment`.
5. Replace **运行输入 JSON** with `{"value":4}` and click **启动工作流**.

Expected result:

- Capability and Workflow are both `parameter-adjustment`.
- Status becomes `completed` without a Provider call.
- Outputs contains the value `10`.
- Cost shows `0 tokens` and zero Provider attempts.
- No Reporting continuation form is shown.
- The persisted state is
  `$MS_DECL_DATA/$MS_PROJECT_ID/Work/runs/parameter-adjustment-*/runtime-state.json`.

Repeat once with `{"value":12}`. Expected Output is `12`, also with zero
Provider attempts. This proves the same generic UI/API runs a non-Reporting
Capability and that business identities have not entered the Kernel.

## B. Declarative Reporting through the browser

Remain on the same server and **工作流** page. Select
`distribution-reporting` and enter this JSON, changing only the instruction if
needed for the real project:

```json
{
  "operation": "full_report",
  "instruction": "基于当前项目 Inputs、Knowledge 和模板生成完整供配电评估报告；保持事实可追溯并明确不确定性。",
  "missing_evidence_policy": "draft",
  "cost_control_mode": "observe",
  "max_provider_attempts": 80,
  "max_total_tokens": 800000
}
```

Click **启动工作流** and record the displayed Run ID. It must start with
`report-declarative-`. Keep the browser open while the Run page polls.

Expected UI behavior:

- Status progresses without manually refreshing and eventually becomes
  `completed`.
- Output and Cost refresh after the terminal state.
- Cost reports non-zero real Provider usage for this Capability.
- Output entries exist and have non-zero sizes.
- The server log contains no uncaught traceback, duplicate terminal transition,
  or repeated execution of an already completed stage.

Record the ID for later commands:

```bash
export MS_DECL_RUN="report-declarative-replace-with-the-displayed-id"
```

Inspect the persisted declarative orchestration and the unchanged Reporting
artifacts:

```bash
test -s "$MS_DECL_DATA/$MS_PROJECT_ID/Work/runs/$MS_DECL_RUN.json"
test -s "$MS_DECL_DATA/$MS_PROJECT_ID/Work/runs/$MS_DECL_RUN/runtime-state.json"
test -s "$MS_DECL_DATA/$MS_PROJECT_ID/Work/runs/$MS_DECL_RUN/resolved-plan.json"
test -s "$MS_DECL_DATA/$MS_PROJECT_ID/Work/runs/$MS_DECL_RUN/workflow-events.jsonl"
test -s "$MS_DECL_DATA/$MS_PROJECT_ID/Work/runs/$MS_DECL_RUN/delivery-receipt.json"
test -s "$MS_DECL_DATA/$MS_PROJECT_ID/Work/runs/$MS_DECL_RUN/delivery-completion.json"
test -s "$MS_DECL_DATA/$MS_PROJECT_ID/Outputs/Reports/证据与来源索引.docx"
find "$MS_DECL_DATA/$MS_PROJECT_ID/Outputs/Reports" -maxdepth 1 -type f -print
find "$MS_DECL_DATA/$MS_PROJECT_ID/Outputs/Modules" -maxdepth 1 -type f -print

jq -e '
  .status == "completed" and
  .workflow_id == "distribution-reporting" and
  .subworkflow_states["run-module-cohort"].status == "completed" and
  .subworkflow_states["run-reporting-tail"].status == "completed" and
  .subworkflow_states["run-reporting-tail"].subworkflow_states["run-cross"].status == "completed" and
  .subworkflow_states["run-reporting-tail"].subworkflow_states["run-chief"].status == "completed" and
  .subworkflow_states["run-reporting-tail"].subworkflow_states["run-final"].status == "completed" and
  .subworkflow_states["run-reporting-tail"].subworkflow_states["run-delivery"].status == "completed"
' "$MS_DECL_DATA/$MS_PROJECT_ID/Work/runs/$MS_DECL_RUN/runtime-state.json"

jq -e '
  .workflow_id == "distribution-reporting" and
  [.actions[].id] == [
    "run-module-cohort",
    "select-reporting-tail",
    "run-reporting-tail",
    "finish-reporting"
  ]
' "$MS_DECL_DATA/$MS_PROJECT_ID/Work/runs/$MS_DECL_RUN/resolved-plan.json"

jq -r '
  select(.kind == "action.completed") |
  [.workflow_id, .action_id] |
  @tsv
' "$MS_DECL_DATA/$MS_PROJECT_ID/Work/runs/$MS_DECL_RUN/workflow-events.jsonl"
```

All `test -s` commands must exit `0`. `Outputs/Modules` must contain modules
2.1 through 2.5. Both `jq -e` commands must exit `0`. There is exactly one
authoritative Kernel `runtime-state.json`; its nested Subworkflow states must
show the module cohort and ordered Cross → Chief → Final → Delivery work as
completed. The event listing must contain the corresponding declared Workflow
and action completions in that order; there must be no separate
`--reporting-module-stage` or `--reporting-tail` state authority.

## C. Legacy paired run through the real server API

Stop terminal A with `Ctrl+C`. Start the isolated Legacy server in terminal B:

```bash
MANYSELVES_DATA_ROOT="$MS_LEGACY_DATA" \
MANYSELVES_INITIAL_PROJECT_ID="$MS_PROJECT_ID" \
MANYSELVES_ADMIN_USERNAME=admin \
MANYSELVES_ADMIN_PASSWORD='replace-with-a-test-password' \
uv run python run_web.py --host 127.0.0.1 --port 9091 --data-dir "$MS_LEGACY_DATA"
```

First use <http://127.0.0.1:9091/> to configure and successfully test the same
Provider/model in **模型设置**. Then stop and restart this Legacy server once so
the API procedure begins without a browser-held process-local control lease.
Do not reopen the browser during the following curl sequence.

In terminal C:

```bash
export MS_COOKIE_JAR="$MS_FINAL_TEST_ROOT/legacy-cookie.txt"
export MS_PASSWORD='replace-with-the-same-test-password'

curl --fail-with-body -sS -c "$MS_COOKIE_JAR" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$MS_PASSWORD\"}" \
  http://127.0.0.1:9091/api/v1/auth/login

curl --fail-with-body -sS -b "$MS_COOKIE_JAR" \
  -H 'Content-Type: application/json' \
  -d '{"clientId":"final-runtime-legacy","actorId":"admin"}' \
  http://127.0.0.1:9091/api/v1/control/lease \
  > "$MS_FINAL_TEST_ROOT/legacy-lease.json"

export MS_LEASE_TOKEN="$(jq -r .leaseToken "$MS_FINAL_TEST_ROOT/legacy-lease.json")"
export MS_COMMAND_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"

curl --fail-with-body -sS -b "$MS_COOKIE_JAR" \
  -H 'Content-Type: application/json' \
  -H "X-Control-Lease: $MS_LEASE_TOKEN" \
  -H "Idempotency-Key: $MS_COMMAND_ID" \
  -d '{"operation":"full_report","instruction":"基于当前项目 Inputs、Knowledge 和模板生成完整供配电评估报告；保持事实可追溯并明确不确定性。","missingEvidencePolicy":"draft","maxProviderAttempts":80,"maxTotalTokens":800000}' \
  http://127.0.0.1:9091/api/v1/reporting/runs \
  | tee "$MS_FINAL_TEST_ROOT/legacy-start.json"

export MS_LEGACY_RUN="$(jq -r .runId "$MS_FINAL_TEST_ROOT/legacy-start.json")"
```

`MS_LEGACY_RUN` must start with `report-` and must not start with
`report-declarative-`. Poll without acquiring another lease:

```bash
curl --fail-with-body -sS -b "$MS_COOKIE_JAR" \
  "http://127.0.0.1:9091/api/v1/reporting/runs/$MS_LEGACY_RUN" \
  | tee "$MS_FINAL_TEST_ROOT/legacy-snapshot.json" \
  | jq '{run: .run, outputs: .outputs}'
```

Repeat that GET until the status is terminal. Expected status is `completed`,
with readable non-empty outputs. Do not start a replacement Run if it fails;
preserve this Run and follow the failure collection section below.

## D. Behavioral-equivalence and document inspection

The paired test passes on behavior and contracts, not on byte-identical prose
or identical token counts. Inspect both isolated workspaces.

For each Run, verify:

- `Work/runs/<run-id>.json` has `status: completed` and non-empty readable
  `output_paths`.
- `delivery-receipt.json` and `delivery-completion.json` belong to that same
  Run, not to an older Run.
- All five module outputs 2.1–2.5 exist.
- Responsibility review/revision/recheck records exist where the Run required
  them; unchanged modules are not spuriously revised.
- Cross review, Chief editing, final review, render, and delivery complete in
  the same order.
- Structured-submit correction, Schema correction, Max Token continuation,
  Tool Slice continuation, No-progress handling, and completed Tool Result
  reuse remain visible if naturally exercised; no original Conversation
  correction or Session identity is replaced.
- A completed same-Run result is reused rather than replayed.
- Cost/usage records contain real Provider attempts and tokens and stay within
  the configured budgets.

Open each final DOCX in Word or LibreOffice and check:

1. The file opens without a repair warning.
2. Required report chapters and modules 2.1–2.5 are present.
3. Tables, headings, images, page breaks, and the contents page are usable.
4. Claims are traceable to the source/evidence indexes.
5. Missing facts are marked as uncertainty rather than invented.
6. The evidence/source index DOCX opens and matches the report's citations.

The declarative and Legacy reports may differ in wording because the Provider
is nondeterministic. They must agree on the project scope, required module set,
workflow stages, delivery contract, and evidence/uncertainty policy.

## E. Persisted recovery after a safe terminal wait

This scenario must never stop the server while a Provider attempt is in flight.
Use a separate declarative Run with a project copy known to be missing at least
one required evidence item and set `"missing_evidence_policy":"ask"`.

1. Start it from **工作流** and record its `report-declarative-*` Run ID.
2. Wait until the UI/API explicitly reports `needs_decision` and `active: false`.
3. Only then stop the server with `Ctrl+C`.
4. Restart the same command with the same `MS_DECL_DATA` and `MS_PROJECT_ID`.
5. Do not reopen the browser yet. Log in and acquire a new process-local control
   lease on port `9092`:

```bash
export MS_RECOVERY_COOKIE_JAR="$MS_FINAL_TEST_ROOT/recovery-cookie.txt"

curl --fail-with-body -sS -c "$MS_RECOVERY_COOKIE_JAR" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$MS_PASSWORD\"}" \
  http://127.0.0.1:9092/api/v1/auth/login

curl --fail-with-body -sS -b "$MS_RECOVERY_COOKIE_JAR" \
  -H 'Content-Type: application/json' \
  -d '{"clientId":"final-runtime-recovery","actorId":"admin"}' \
  http://127.0.0.1:9092/api/v1/control/lease \
  > "$MS_FINAL_TEST_ROOT/recovery-lease.json"

export MS_RECOVERY_LEASE_TOKEN="$(jq -r .leaseToken "$MS_FINAL_TEST_ROOT/recovery-lease.json")"
```

6. GET the same Run and confirm it still reports the pending `waitingInput` and
   input/decision ID:

```bash
export MS_RECOVERY_RUN="report-declarative-replace-with-the-waiting-run-id"

curl --fail-with-body -sS -b "$MS_RECOVERY_COOKIE_JAR" \
  "http://127.0.0.1:9092/api/v1/runs/$MS_RECOVERY_RUN" \
  | tee "$MS_FINAL_TEST_ROOT/recovery-before-resume.json" \
  | jq '{run: .run, waitingInput: .waitingInput}'

export MS_RECOVERY_INPUT_ID="$(jq -r '
  .waitingInput[0].input_id //
  .waitingInput[0].inputId //
  .waitingInput[0].decision_id //
  .waitingInput[0].decisionId //
  empty
' "$MS_FINAL_TEST_ROOT/recovery-before-resume.json")"
test -n "$MS_RECOVERY_INPUT_ID"
```

7. Resume that decision with the same Run ID:

```bash
export MS_RECOVERY_COMMAND_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"

curl --fail-with-body -sS -b "$MS_RECOVERY_COOKIE_JAR" \
  -H 'Content-Type: application/json' \
  -H "X-Control-Lease: $MS_RECOVERY_LEASE_TOKEN" \
  -H "Idempotency-Key: $MS_RECOVERY_COMMAND_ID" \
  -d "{\"inputId\":\"$MS_RECOVERY_INPUT_ID\",\"values\":{\"action\":\"draft\",\"supplements\":[]}}" \
  "http://127.0.0.1:9092/api/v1/runs/$MS_RECOVERY_RUN/input"
```

The accepted response and every later snapshot must retain exactly
`MS_RECOVERY_RUN`. It must not create a Legacy `report-*` replacement, replay a
completed stage, or lose the author/auditor Conversations. The Run must either
complete normally or expose a specific preserved failure on that same Run.

## Pass criteria

The final real test passes only when all of these are true:

- Neutral values 4 and 12 complete as 10 and 12 with zero Provider usage.
- The browser starts and follows a real declarative Reporting Run to completion.
- The declarative Run ID has the `report-declarative-*` prefix.
- The Reporting-specific API starts the isolated Legacy Run by default and its
  ID does not have the declarative prefix.
- Both paired Runs complete and produce readable current-Run DOCX, module,
  review, receipt, and index artifacts.
- The single declarative resolved plan and authoritative Runtime state exist;
  its nested module cohort and tail stages complete in declared order.
- Recovery retains the same declarative Run and its persisted decision,
  Conversations, completed results, and remaining work.
- Provider usage and cost are visible, non-zero for Reporting, and not silently
  duplicated by declarative orchestration.
- No new hash/CAS record or acceptance Gate is required to run or recover.
- The Legacy runner remains available and remains the default Reporting route.

## Fail criteria and evidence collection

Any one of these is a failure:

- Wrong engine prefix, missing Capability, or generic UI cannot start the Run.
- A Run reports `completed` while a declared output is absent or unreadable.
- Missing/repeated module, Cross, Chief, Final, Render, or Delivery work.
- Lost Conversation/Session identity, lost Schema correction, lost continuation,
  lost No-progress handling, or re-executed completed Tool Result.
- Restart creates a new Run or loses the pending decision/checkpoint.
- Neutral Capability calls a Provider or imports Reporting behavior.
- Server traceback, browser error, unusable DOCX, or cost projection mismatch.

On failure, do not automatically restart, resume, delete, or overwrite the Run.
Preserve and report:

```text
branch and HEAD
server command and complete server log
browser screenshot and browser console/network errors
Run ID and terminal status
Work/runs/<run-id>.json
Work/runs/<run-id>/
Outputs/
the request JSON and Provider/model name (never the API Key)
```

## Rollback

1. Stop the test server with `Ctrl+C`.
2. Keep both test data roots intact for diagnosis.
3. For immediate operational fallback, use the existing
   `/api/v1/reporting/runs` route; it is still Legacy by default.
4. To run only the pre-implementation code, use a separate checkout of
   `agent/declarative-runtime-plan`; do not reset or delete this implementation
   branch or either evidence directory.

No data migration is required to roll back because the test uses isolated data
roots and the default Reporting route was not switched.
