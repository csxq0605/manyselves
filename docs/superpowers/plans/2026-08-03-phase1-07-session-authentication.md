# Phase 1 Session Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the user-visible deployment bearer token with a server-validated `admin / yuanxi@2026` login page and HttpOnly session cookie.

**Architecture:** A small stdlib HMAC session signer owns a persistent server-only key under the data root. FastAPI exposes login/session/logout routes and protects all business routes with one cookie dependency; React boots through an `AuthGate` and sends same-origin credentials without ever reading a token.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, HMAC-SHA256, React 19, React Router, TanStack Query, Vitest, Playwright.

## Global Constraints

- Opening `http://192.168.8.28:9090/` while unauthenticated must show the login page.
- Default credentials are exactly `admin / yuanxi@2026`, configured only on the server.
- No Access Token field, bearer token storage, or Authorization injection may remain in React.
- Cookie must be HttpOnly and SameSite=Strict; HTTP LAN mode is not presented as public-internet secure.
- Keep one FastAPI Runtime and one Gunicorn Uvicorn worker.
- Do not add a database, user table, RBAC, Redis, or an external auth package.

---

### Task 1: Server session primitive and auth routes

**Files:**
- Create: `manyselves/webapi/session_auth.py`
- Create: `manyselves/webapi/schemas/auth.py`
- Create: `manyselves/webapi/routes/auth.py`
- Modify: `manyselves/webapi/settings.py`
- Modify: `manyselves/webapi/lifespan.py`
- Modify: `manyselves/webapi/main.py`
- Test: `tests/webapi/test_auth.py`

**Interfaces:**
- Produces: `SessionPrincipal(username: str, expires_at: int)`.
- Produces: `SessionSigner(key_path: Path, ttl_seconds: int)` with `issue(username: str) -> str` and `verify(value: str) -> SessionPrincipal | None`.
- Produces: `SESSION_COOKIE_NAME = "manyselves_session"`.
- Produces: `POST /api/v1/auth/login`, `GET /api/v1/auth/session`, `POST /api/v1/auth/logout`.

- [x] **Step 1: Write failing signer and route tests**

```python
def test_session_signer_rejects_tampering(tmp_path: Path) -> None:
    signer = SessionSigner(tmp_path / "session.key", ttl_seconds=3600, now=lambda: 100)
    value = signer.issue("admin")
    assert signer.verify(value).username == "admin"
    assert signer.verify(value + "x") is None

@pytest.mark.asyncio
async def test_login_sets_http_only_cookie(async_client: httpx.AsyncClient) -> None:
    response = await async_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "yuanxi@2026"},
    )
    assert response.status_code == 204
    assert "manyselves_session=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
```

- [x] **Step 2: Run the focused tests and verify the missing-module failure**

Run: `uv run pytest tests/webapi/test_auth.py -q`

Expected: FAIL because `manyselves.webapi.session_auth` and the auth routes do not exist.

- [x] **Step 3: Implement the signer, settings, schemas, and routes**

Add these settings and contracts exactly:

```python
class WebSettings(BaseSettings):
    data_root: Path
    initial_project_id: str
    admin_username: str = "admin"
    admin_password: SecretStr = SecretStr("yuanxi@2026")
    session_ttl_seconds: int = 12 * 60 * 60
    session_cookie_secure: bool = False

class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: SecretStr

class SessionResponse(BaseModel):
    authenticated: bool
    username: str
    expires_at: datetime = Field(alias="expiresAt")
```

Implement a base64url JSON payload containing username and expiry, signed with HMAC-SHA256. Create the key file with `secrets.token_bytes(32)`, exclusive creation, user-only permissions where supported, and atomic read-after-create handling. Compare usernames, passwords, and signatures with `secrets.compare_digest`. Never include the password or key in exceptions, reprs, or API responses.

Initialize one signer during lifespan at:

```python
settings.data_root / ".manyselves" / "auth" / "session.key"
```

Login returns 204 and sets the cookie; session returns 200 for a valid cookie and 401 `AUTH_REQUIRED` otherwise; logout always returns 204 and deletes the cookie.

- [x] **Step 4: Run signer and route tests**

Run: `uv run pytest tests/webapi/test_auth.py -q`

Expected: PASS.

- [x] **Step 5: Commit the session primitive**

```bash
git add manyselves/webapi/session_auth.py manyselves/webapi/schemas/auth.py manyselves/webapi/routes/auth.py manyselves/webapi/settings.py manyselves/webapi/lifespan.py manyselves/webapi/main.py tests/webapi/test_auth.py
git commit -m "feat: add server session authentication"
```

### Task 2: Protect business APIs and remove bearer semantics

**Files:**
- Modify: `manyselves/webapi/security.py`
- Modify: `manyselves/webapi/main.py`
- Modify: `manyselves/webapi/routes/control.py`
- Modify: `manyselves/webapi/routes/projects.py`
- Modify: `manyselves/webapi/routes/files.py`
- Modify: `manyselves/webapi/routes/conversations.py`
- Modify: `manyselves/webapi/routes/events.py`
- Modify: `manyselves/webapi/routes/agents.py`
- Modify: `manyselves/webapi/routes/reporting.py`
- Modify: `manyselves/webapi/routes/settings.py`
- Modify: `manyselves/webapi/routes/operations.py`
- Modify: `manyselves/webapi/routes/maintenance.py`
- Create: `tests/webapi/auth_helpers.py`
- Modify: `tests/webapi/test_security_and_control.py`
- Modify: `tests/webapi/test_projects_and_files.py`
- Modify: `tests/webapi/test_conversations_agents_reporting.py`
- Modify: `tests/webapi/test_sse.py`
- Modify: `tests/webapi/test_lifecycle.py`
- Modify: `tests/webapi/test_openapi_contract.py`
- Modify: `scripts/export_openapi.py`

**Interfaces:**
- Consumes: `SessionSigner.verify()` and `SESSION_COOKIE_NAME` from Task 1.
- Produces: `require_authenticated_session(request: Request) -> SessionPrincipal`.
- Produces: cookie-based OpenAPI security scheme `SessionCookie`.

- [x] **Step 1: Replace bearer characterization tests with cookie characterization tests**

```python
async def login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "yuanxi@2026"},
    )
    assert response.status_code == 204

@pytest.mark.asyncio
async def test_business_route_rejects_missing_session(async_client: httpx.AsyncClient) -> None:
    response = await async_client.get("/api/v1/projects")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
```

Update all web API fixtures to call `login(client)` instead of assigning an Authorization header. Health and auth routes remain anonymous; bootstrap, projects, files, conversations, events, agents, reporting, settings, operations, maintenance, and control require a session.

- [x] **Step 2: Run the WebAPI suite and verify bearer-dependent failures**

Run: `uv run pytest tests/webapi -q`

Expected: FAIL on missing cookie dependency and obsolete bearer OpenAPI assertions.

- [x] **Step 3: Implement cookie dependency and route protection**

Replace `require_deployment_access` with:

```python
def require_authenticated_session(request: Request) -> SessionPrincipal:
    value = request.cookies.get(SESSION_COOKIE_NAME)
    principal = request.app.state.session_signer.verify(value or "")
    if principal is None:
        raise ApiError(
            status_code=401,
            code="AUTH_REQUIRED",
            message="An authenticated session is required",
            retryable=False,
        )
    return principal
```

Apply the dependency at router level wherever possible so read and mutation routes are protected consistently. Keep `X-Control-Lease-Token` as the independent Runtime mutation-control mechanism. Replace `DeploymentBearer` OpenAPI rewriting with a cookie API-key scheme named `SessionCookie` using cookie name `manyselves_session`.

- [x] **Step 4: Regenerate and verify the API contract**

Run: `uv run python scripts/export_openapi.py`

Run: `uv run pytest tests/webapi -q`

Expected: PASS, with no `DeploymentBearer` or Authorization requirement in `frontend-contract/openapi.json`.

- [x] **Step 5: Commit cookie protection and contract changes**

```bash
git add manyselves/webapi tests/webapi scripts/export_openapi.py frontend-contract/openapi.json
git commit -m "refactor: protect APIs with session cookies"
```

### Task 3: React AuthGate, login page, and credential-free transport

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/src/main.tsx`
- Modify: `frontend/src/app/App.tsx`
- Create: `frontend/src/features/auth/auth-api.ts`
- Create: `frontend/src/features/auth/AuthGate.tsx`
- Create: `frontend/src/features/auth/LoginPage.tsx`
- Create: `frontend/src/features/auth/login.css`
- Create: `frontend/src/features/auth/AuthGate.test.tsx`
- Create: `frontend/src/features/auth/LoginPage.test.tsx`
- Modify: `frontend/src/api/gateway.ts`
- Modify: `frontend/src/api/gateway.test.ts`
- Modify: `frontend/src/api/event-stream.ts`
- Modify: `frontend/src/api/event-stream.test.ts`
- Modify: `frontend/src/features/settings/settings-storage.ts`
- Modify: `frontend/src/features/settings/settings-storage.test.ts`
- Delete: `frontend/src/features/settings/ServerConnectionForm.tsx`
- Delete: `frontend/src/features/settings/ServerConnectionForm.test.tsx`

**Interfaces:**
- Consumes: `/api/v1/auth/login|session|logout` from Task 1.
- Produces: `AuthApi` with `login`, `session`, and `logout` methods.
- Produces: `AuthGate` that renders `LoginPage` before bootstrapping Runtime state.
- Produces: gateway and SSE requests using `credentials: "same-origin"` and no token callback.

- [x] **Step 1: Install React Router and lock the dependency**

Run: `npm install react-router-dom@^7 --save`

Working directory: `frontend`

Expected: `package.json` and `package-lock.json` contain `react-router-dom` and no unrelated dependency upgrades.

- [x] **Step 2: Write failing login and transport tests**

```tsx
it("shows the login page before bootstrap when session is absent", async () => {
  render(<AuthGate api={api}><div>应用内容</div></AuthGate>);
  expect(await screen.findByRole("heading", { name: "登录 manyselves" })).toBeVisible();
});

it("sends cookies and never sets Authorization", async () => {
  const gateway = createApiGateway({ baseUrl: "", clientId: "c-1", fetch, getLeaseToken: () => null });
  await gateway.requestJson("/api/v1/projects");
  expect(fetch.mock.calls[0]?.[1]).toMatchObject({ credentials: "same-origin" });
  expect(new Headers(fetch.mock.calls[0]?.[1]?.headers).has("Authorization")).toBe(false);
});
```

- [x] **Step 3: Run focused frontend tests and verify failure**

Run: `npm test -- --run src/features/auth src/api/gateway.test.ts src/api/event-stream.test.ts src/features/settings/settings-storage.test.ts`

Working directory: `frontend`

Expected: FAIL because auth components do not exist and gateway still accepts `getToken`.

- [x] **Step 4: Implement AuthGate and remove token storage**

Define:

```ts
export interface AuthApi {
  session(): Promise<SessionResponse>;
  login(input: { username: string; password: string }): Promise<void>;
  logout(): Promise<void>;
}
```

The login form contains only username, password, and login button, defaults the visible username to `admin`, does not default or embed the password, and maps all credential failures to one non-secret error. `App` must not call bootstrap or start SSE until `AuthGate` reports an authenticated session. Gateway and event stream always use same-origin credentials. Remove `ServerConnection.token`, `tokenKey`, secure token adapters, and every sessionStorage access to `manyselves.deploymentToken`.

- [x] **Step 5: Run focused tests, typecheck, and lint**

Run: `npm test -- --run src/features/auth src/api/gateway.test.ts src/api/event-stream.test.ts src/features/settings/settings-storage.test.ts`

Run: `npm run build`

Run: `npm run lint`

Working directory: `frontend`

Expected: all commands PASS.

- [x] **Step 6: Commit the login shell**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src
git commit -m "feat: add browser login and cookie transport"
```

### Task 4: Deployment scripts and session-authenticated smoke verification

**Files:**
- Modify: `deploy/env.example`
- Modify: `deploy/compose.yaml`
- Modify: `deploy/nginx/default.conf`
- Modify: `deploy/backup/backup.sh`
- Modify: `deploy/backup/backup.ps1`
- Modify: `scripts/verify_deployment.py`
- Modify: `tests/release/test_deployment_verifier.py`
- Modify: `docs/deployment/linux-compose.md`

**Interfaces:**
- Consumes: login cookie contract from Task 1.
- Produces: deployment environment names `MANYSELVES_ADMIN_USERNAME` and `MANYSELVES_ADMIN_PASSWORD`.
- Produces: verifier and backup clients that login once and retain a cookie jar.

- [x] **Step 1: Write failing deployment verifier tests**

```python
def test_verifier_logs_in_before_protected_checks(fake_http: FakeHttp) -> None:
    verify(base_url="http://192.168.8.28:9090", username="admin", password="yuanxi@2026")
    assert fake_http.requests[0].path == "/api/v1/auth/login"
    assert "Authorization" not in fake_http.requests[1].headers
```

- [x] **Step 2: Run release tests and verify token assumptions fail**

Run: `uv run pytest tests/release/test_deployment_verifier.py -q`

Expected: FAIL because the verifier still requires a bearer token.

- [x] **Step 3: Update deployment environment and clients**

Remove `MANYSELVES_ACCESS_TOKEN`. Add the two administrator variables with the approved defaults in `env.example`, pass them only to the API container, and keep Nginx at 9090/API at 9000. Nginx must forward Cookie and Origin normally and no longer needs explicit Authorization forwarding. Python verifier and backup scripts must login, retain cookies, acquire the existing control lease, perform work, and logout/clean up.

- [x] **Step 4: Run deployment and contract checks**

Run: `uv run pytest tests/release/test_deployment_verifier.py tests/webapi/test_openapi_contract.py -q`

Run: `uv run python scripts/verify_deployment.py --help`

Expected: PASS; help names username/password and contains no access-token option.

- [x] **Step 5: Commit deployment auth migration**

```bash
git add deploy scripts/verify_deployment.py tests/release/test_deployment_verifier.py docs/deployment/linux-compose.md
git commit -m "deploy: use administrator login for operations"
```
