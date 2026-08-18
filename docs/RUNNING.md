# Running ManySelves

This document contains the operational commands for the current deployed and stable release. For product positioning and the future domain-stateless kernel roadmap, see the repository [README](../README.md).

## Fastest paths

| Goal | Command |
| --- | --- |
| Local React + FastAPI on Linux/macOS | `cp .env.example .env`, edit it, then `./start.sh` |
| Local React + FastAPI on Windows | build `frontend/`, then run `scripts\start.bat` |
| Local PyQt desktop | `uv sync && uv run manyselves` |
| Server deployment | `docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --build --wait` |
| Health check | `curl --fail http://127.0.0.1:9090/api/v1/health/ready` |
| Stop Compose deployment | `docker compose -f deploy/compose.yaml --env-file deploy/.env down` |

## Requirements

| Mode | Requirements |
| --- | --- |
| React + FastAPI | Python 3.12+, Node.js 22+, npm, model-provider key |
| PyQt desktop | Python 3.12+, `uv`, model-provider key |
| Electron | React build plus `desktop/` dependencies |
| Docker Compose | Docker Compose v2 and a persistent host directory |
| Multi-account | One FastAPI process, an account manifest, and one password environment variable per account |
| Deployment verifier | Python 3.12+, `uv`, and access to the running HTTP service |

Clone the maintained branch:

```bash
git clone --branch feature/react-fastapi-manyselves \
  https://github.com/csxq0605/manyselves.git
cd manyselves
```

## Local React + FastAPI

### Linux/macOS complete startup

```bash
cp .env.example .env
```

Edit `.env` before starting. At minimum, replace the example administrator password and configure a provider key:

```dotenv
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=replace-with-a-long-random-password
MANYSELVES_ALLOWED_ORIGINS='["http://127.0.0.1:9090"]'
MANYSELVES_HTTP_BIND=127.0.0.1
MANYSELVES_HTTP_PORT=9090

# Configure one provider here or later in the Web settings page.
MIMO_API_KEY=
# OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
```

Start:

```bash
chmod +x start.sh
./start.sh
```

The root `start.sh` delegates to `scripts/start.sh`. The script builds `frontend/` when needed, creates `.venv`, installs the Python package, loads `.env`, and starts `run_web.py --reload`.

### Windows complete startup

```bat
copy .env.example .env
```

Edit `.env`. The Windows batch loader should receive the origins value without outer single quotes:

```dotenv
MANYSELVES_ALLOWED_ORIGINS=["http://127.0.0.1:9090"]
```

Build the React client and start the API from the repository root:

```bat
npm --prefix frontend install
npm --prefix frontend run build
scripts\start.bat
```

The Windows entry point is `scripts\start.bat`; there is no root-level `start.bat`.

### Addresses

Repository startup examples use port `9090`:

```text
Application:  http://127.0.0.1:9090
OpenAPI UI:   http://127.0.0.1:9090/docs
Live health:  http://127.0.0.1:9090/api/v1/health/live
Ready health: http://127.0.0.1:9090/api/v1/health/ready
```

### API-focused development

```bash
uv sync
npm --prefix frontend install
npm --prefix frontend run build

uv run python run_web.py \
  --reload \
  --host 127.0.0.1 \
  --port 9090 \
  --data-dir .manyselves
```

`run_web.py` defaults to port `9000`, so set `--port 9090` explicitly when the browser URL, origin configuration, or verification scripts expect port `9090`.

Useful arguments:

```text
--host  --port  --workers  --reload
--data-dir  --log-level  --accounts-file
```

## Local PyQt desktop

```bash
uv sync
cp manyselves.config.example.yaml manyselves.config.yaml
uv run manyselves
```

The application can open without a provider key, but model calls require an enabled provider. Configure the provider in the application or through a supported environment variable.

## Electron

The Electron client packages the built React application and connects to the ManySelves FastAPI service.

Build the React assets and Electron main process:

```bash
npm --prefix frontend install
npm --prefix frontend run build

cd desktop
npm install
npm run build
npx electron .
```

Create an unpacked distributable:

```bash
cd desktop
npm run package
```

The unpacked output is written below `desktop/release/`.

## Multi-account mode

Copy and protect the account manifest:

```bash
cp deploy/accounts.example.yaml deploy/accounts.yaml
chmod 600 deploy/accounts.yaml
```

Set the password variables named by `passwordEnv` in the manifest:

```bash
export MANYSELVES_ACCOUNT_A_PASSWORD='replace-with-a-long-random-password'
export MANYSELVES_ACCOUNT_B_PASSWORD='replace-with-a-long-random-password'
```

Start one FastAPI process:

```bash
uv sync

uv run python run_web.py \
  --accounts-file deploy/accounts.yaml \
  --workers 1 \
  --host 127.0.0.1 \
  --port 9090 \
  --data-dir .manyselves
```

Multi-account mode requires `--workers 1`. Each account runtime is created lazily and writes below:

```text
.manyselves/accounts/<account-id>/
```

Provider keys can be configured for each account after login. Do not place real account manifests or passwords in source control.

## Specialized split-process headless reporting

This entry point separates a reporting API process from a worker process. It is not required for normal PyQt, React + FastAPI, Electron, or Compose startup.

```bash
export MANYSELVES_PROJECT_STORAGE_ROOT=/srv/manyselves/projects
export MANYSELVES_SERVICE_STATE_ROOT=/srv/manyselves/state
export MANYSELVES_CONFIG_ROOT=/srv/manyselves/config

# API process
uv run manyselves-headless api --host 127.0.0.1 --port 8080

# Worker process, started separately
export MANYSELVES_PROVIDER_API_KEY='replace-me'
uv run manyselves-headless worker
```

Provider type, base URL, model, secret variable name, and worker ID can be selected with:

```text
MANYSELVES_PROVIDER_TYPE
MANYSELVES_PROVIDER_API_BASE
MANYSELVES_PROVIDER_MODEL
MANYSELVES_PROVIDER_API_KEY_SECRET
MANYSELVES_WORKER_ID
```

## Docker Compose deployment

### Prepare the environment

```bash
cp deploy/env.example deploy/.env
chmod 600 deploy/.env
```

Edit `deploy/.env`. The deployment requires an explicit administrator password and persistent host data directory:

```dotenv
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=replace-with-a-long-random-password
MANYSELVES_DATA_DIR=/srv/manyselves/data

MANYSELVES_HTTP_BIND=127.0.0.1
MANYSELVES_HTTP_PORT=9090
MANYSELVES_ALLOWED_ORIGINS=["http://127.0.0.1:9090"]
```

For a public hostname behind a reverse proxy, set the exact HTTPS origin. For direct network exposure, set an appropriate bind address and protect the service with a firewall and HTTPS.

Create the persistent host directory before starting:

```bash
sudo install -d -m 0750 /srv/manyselves/data
```

The container must be able to write this directory. On SELinux-enabled hosts, the Compose volume already applies the `:Z` label.

### Validate and start

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env config

docker compose -f deploy/compose.yaml --env-file deploy/.env \
  up -d --build --wait
```

### Daily operations

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env ps

docker compose -f deploy/compose.yaml --env-file deploy/.env \
  logs -f --tail 200 api web

docker compose -f deploy/compose.yaml --env-file deploy/.env restart

docker compose -f deploy/compose.yaml --env-file deploy/.env down
```

After changing `deploy/.env`, recreate the affected container so it receives the new process environment:

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env \
  up -d --force-recreate api
```

## Verify a deployment

### Health checks

```bash
curl --fail http://127.0.0.1:9090/api/v1/health/live
curl --fail http://127.0.0.1:9090/api/v1/health/ready
```

### Authenticated public-API drill

From a source checkout with `uv`:

```bash
uv sync

set -a
. deploy/.env
set +a

uv run python scripts/verify_deployment.py \
  --url http://127.0.0.1:9090 \
  --username "$MANYSELVES_ADMIN_USERNAME" \
  --password-env MANYSELVES_ADMIN_PASSWORD
```

The verifier checks:

- administrator login and cookie reuse;
- live and ready health endpoints;
- runtime and event-stream availability;
- project and conversation APIs;
- a reversible file create/read/download/delete round trip;
- SSE response type;
- control-lease cleanup and logout.

The temporary verifier file is removed during cleanup.

## Backup and restore

### Backup

```bash
set -a
. deploy/.env
set +a

export MANYSELVES_API_URL=http://127.0.0.1:9090
deploy/backup/backup.sh /srv/manyselves/backups
```

Keep the backup directory outside the active `MANYSELVES_DATA_DIR`.

### Restore

Restore is an offline operation:

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env down
export MANYSELVES_SERVICES_STOPPED=yes

deploy/backup/restore.sh \
  /srv/manyselves/backups/manyselves-YYYYMMDDTHHMMSSZ.tar.gz \
  --force

docker compose -f deploy/compose.yaml --env-file deploy/.env \
  up -d --wait
```

Run the deployment verifier after restore or rollback.

## Development checks

```bash
uv sync
QT_QPA_PLATFORM=offscreen uv run pytest -q
uv run ruff check manyselves tests scripts
npm --prefix frontend run verify
npm --prefix desktop run verify
```

## Troubleshooting

- **No provider available:** the service can start and the workspace can open, but Agent calls require an enabled provider with a valid key.
- **Port conflict:** run `lsof -i :9090`, stop the conflicting process, or select another port and update the allowed origin.
- **Stale or missing frontend:** remove `frontend/dist`, then run `npm --prefix frontend install` and `npm --prefix frontend run build`.
- **Windows Web start shows no UI:** build `frontend/` before running `scripts\start.bat`.
- **Rejected account manifest:** require `version: 1`, unique IDs and usernames, valid `passwordEnv` values, mode `600` on non-Windows, and `--workers 1`.
- **Environment change not visible:** restart the local process or recreate the container. A browser refresh cannot replace server environment variables.
- **Data permission error:** make the data directory writable by the service account and keep one authoritative write root per account or deployment.
- **Health is live but not ready:** inspect API logs and verify the runtime data directory, account manifest, and provider/configuration state.

## Security

- Replace every example password before shared, server, or networked use.
- Keep API keys, active `.env` files, and account manifests out of source control.
- Bind to loopback by default; use HTTPS and an exact allowed origin on networked deployments.
- Protect the data root and backup directory with operating-system permissions.
- Do not attach multiple API replicas or unrelated processes to the same authoritative runtime write root.
