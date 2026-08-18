# Phase 1 Codex Light Redesign Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute the approved Phase 1 light Web redesign through five bounded, reviewable plans without changing the established Agent/Loop/Reporting architecture.

**Architecture:** Plans 07-10 each deliver one independently testable subsystem; Plan 11 integrates and releases them. Progress is recorded only after the plan's tests and commit succeed, so design approval, plan completion, code implementation, and deployment verification cannot be confused.

**Tech Stack:** React 19, React Router, TanStack Query, FastAPI, Gunicorn/Uvicorn, filesystem persistence, pytest, Vitest, Playwright, Podman Compose.

## Global Constraints

- Implementation has not started when this master plan is created.
- Execute plans in the listed order; do not combine unrelated tasks into one commit.
- Use TDD for every code task and run each plan's focused verification before its review gate.
- Keep API 9000, Nginx 9090, single Runtime, single worker, and existing project directory compatibility.
- Do not add Redis, MySQL, Milvus, MinIO, LLM Wiki, RBAC, multi-tenant scheduling, or a server-file browser.
- Never mark a plan complete because code was written; mark it complete only after its required tests pass and its commits exist.

---

## Execution order and gates

- [x] **Plan 07 — Session authentication**

Plan: `docs/superpowers/plans/2026-08-03-phase1-07-session-authentication.md`

Gate: login/session/logout, protected REST/SSE, credential-free React transport, deployment/backup scripts, and focused backend/frontend/release tests pass.

- [ ] **Plan 08 — Project workspace UI**

Plan: `docs/superpowers/plans/2026-08-03-phase1-08-project-workspace-ui.md`

Gate: light routed shell, project metadata, complete six-entry tree, capability-aware files, browser-computer uploads, project-bound conversations, Runtime/Logs/Outputs views, and focused E2E pass.

- [ ] **Plan 09 — Global knowledge**

Plan: `docs/superpowers/plans/2026-08-03-phase1-09-global-knowledge.md`

Gate: global knowledge CRUD, safe composite retrieval, project priority, run-stable provenance, global knowledge page, reporting regression, and focused E2E pass.

- [ ] **Plan 10 — Simple model settings**

Plan: `docs/superpowers/plans/2026-08-03-phase1-10-simple-model-settings.md`

Gate: credential ownership, no environment-secret YAML leak, connection test, simple light settings UI, YAML documentation, and settings/recovery tests pass.

- [ ] **Plan 11 — Integration and release**

Plan: `docs/superpowers/plans/2026-08-03-phase1-11-integration-release.md`

Gate: generated contracts, complete E2E, full Python/frontend regression, security/compatibility gates, clean image builds, isolated Podman smoke, deployment verifier, evidence document, and final completion review pass.

## Progress update format

After each task, update the owning plan checkbox and report exactly:

```text
Task: <plan/task>
State: completed | failed | blocked
Commit: <sha or none>
Tests: <exact command and result>
Remaining in current plan: <task numbers>
```

Do not report later plans as started while an earlier gate is incomplete unless the user explicitly authorizes a documented parallel execution strategy.
