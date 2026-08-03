# Phase 1 IP and Port Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Phase 1 LAN deployment use private API port 9000, Nginx/host port 9090, and default client origin `http://192.168.8.28:9090` without changing runtime or persistence behavior.

**Architecture:** The web container is the only published service. It listens on 9090 and proxies API/SSE traffic over the Compose network to `api:9000`; Gunicorn and its health check share port 9000. LAN clients use the server IP directly and authenticate with the existing deployment token.

**Tech Stack:** Docker Compose, Gunicorn, UvicornWorker, Nginx, pytest, Docker health checks.

## Global Constraints

- Keep exactly one Gunicorn worker and one API replica.
- Never publish API port 9000 on the host.
- Publish Nginx on `0.0.0.0:9090` for the trusted LAN only.
- Use the exact default Origin and client URL `http://192.168.8.28:9090`.
- Do not modify application APIs, runtime core, persistence formats, React features, or Electron IPC.

---

### Task 1: Lock the New Network Contract with Failing Tests

**Files:**
- Modify: `tests/deploy/test_api_image_contract.py`
- Modify: `tests/deploy/test_compose_contract.py`

**Interfaces:**
- Consumes: deployment text/YAML artifacts.
- Produces: executable assertions for ports, bind address, origin, and proxy topology.

- [ ] **Step 1: Add API port assertions**

Require `--bind 0.0.0.0:9000`, API Dockerfile `EXPOSE 9000`, and health check URL `127.0.0.1:9000`.

- [ ] **Step 2: Add Compose/Nginx/default-client assertions**

Require API `expose: ["9000"]`, web mapping default `0.0.0.0:9090:9090`, Nginx `listen 9090`, two `proxy_pass http://api:9000` entries, Origin `http://192.168.8.28:9090`, and Electron default URL with the same address.

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/deploy/test_api_image_contract.py tests/deploy/test_compose_contract.py -q`

Expected: FAIL on the existing 8000/8080/localhost values.

### Task 2: Implement the 9000/9090/IP Deployment Contract

**Files:**
- Modify: `deploy/api/entrypoint.sh`
- Modify: `deploy/api/healthcheck.py`
- Modify: `deploy/api/Dockerfile`
- Modify: `deploy/nginx/default.conf`
- Modify: `deploy/web/Dockerfile`
- Modify: `deploy/compose.yaml`
- Modify: `deploy/env.example`
- Modify: `frontend/src/main.tsx`

**Interfaces:**
- Consumes: the fixed values from Task 1.
- Produces: one deployable Compose topology at `http://192.168.8.28:9090`.

- [ ] **Step 1: Change the private API port**

Set Gunicorn bind, API EXPOSE, API health URL, Compose expose, and both Nginx upstreams to 9000.

- [ ] **Step 2: Change the Nginx and host port**

Set Nginx listen/EXPOSE/container target to 9090, with Compose defaults `MANYSELVES_HTTP_BIND=0.0.0.0` and `MANYSELVES_HTTP_PORT=9090`.

- [ ] **Step 3: Change IP/Origin defaults**

Set Compose fallback origin, `deploy/env.example`, and Electron default server URL to `http://192.168.8.28:9090`.

- [ ] **Step 4: Verify GREEN**

Run: `uv run pytest tests/deploy/test_api_image_contract.py tests/deploy/test_compose_contract.py tests/release/test_security_boundaries.py -q`

Expected: PASS.

### Task 3: Update Operator Documentation

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `docs/deployment/linux-compose.md`
- Modify: `docs/deployment/electron-client.md`
- Modify: `docs/phase1/linux-pilot-checklist.md`
- Modify: `docs/phase1/implementation-status.md`

**Interfaces:**
- Consumes: the deployed values from Task 2.
- Produces: copyable no-domain LAN deployment and client instructions.

- [ ] **Step 1: Replace active deployment examples**

Use `http://192.168.8.28:9090` for browser, Electron, health, backup API URL, and deployment-verifier commands. State that 9000 is internal-only and 9090 must be restricted to the trusted LAN.

- [ ] **Step 2: Check active references**

Run: `rg -n "localhost:8080|127\\.0\\.0\\.1:8080|api:8000|listen 8080|EXPOSE 8080" deploy README.md README_zh.md docs/deployment docs/phase1 frontend/src/main.tsx`

Expected: no active deployment matches.

### Task 4: Build and Verify the Real Stack

**Files:**
- Verify: `deploy/compose.yaml`
- Verify: `scripts/verify_deployment.py`

**Interfaces:**
- Consumes: final deployment configuration.
- Produces: fresh build, health, process-count, and 8/8 HTTP verifier evidence.

- [ ] **Step 1: Render and build Compose**

Run `docker compose -f deploy/compose.yaml config --quiet` with test environment variables, then `docker compose ... build`.

- [ ] **Step 2: Start and inspect**

Run `docker compose ... up -d --wait`, curl both health endpoints through `http://127.0.0.1:9090`, and confirm one Gunicorn master plus one worker with `docker top`.

- [ ] **Step 3: Run the public verifier**

Run `scripts/verify_deployment.py --url http://127.0.0.1:9090` with the temporary deployment token.

Expected: all eight checks are true.

- [ ] **Step 4: Run regression gates and clean up**

Run deployment/release tests, Ruff on changed Python tests/scripts, `git diff --check`, then `docker compose down`. Preserve images and data; remove only test containers/network.

- [ ] **Step 5: Commit**

```bash
git add deploy frontend/src/main.tsx tests/deploy README.md README_zh.md docs/deployment docs/phase1
git diff --cached --check
git commit -m "build: use phase one lan ports"
```
