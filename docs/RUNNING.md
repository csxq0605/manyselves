# Running ManySelves

This document is the operational reference for the React/FastAPI runtime, Electron shell, multi-account service, and container deployment. The repository README describes the kernel, capability definitions, durable state, and scenario-demo boundaries.

## Fastest paths

| Goal | Command |
| --- | --- |
| Local React + FastAPI on Linux/macOS | `cp .env.example .env`, edit it, then `./start.sh` |
| Local React + FastAPI on Windows | build `frontend/`, then run `scripts\start.bat` |
| API-focused process | `uv run python run_web.py --reload --host 127.0.0.1 --port 9090` |
| Compose deployment | `docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --build --wait` |
| Ready check | `curl --fail http://127.0.0.1:9090/api/v1/health/ready` |
| Stop Compose | `docker compose -f deploy/compose.yaml --env-file deploy/.env down` |

## Requirements

| Mode | Requirements |
| --- | --- |
| React + FastAPI | Python 3.12+, Node.js 22+, npm, model-provider key |
| Electron | built React assets plus `desktop/` dependencies |
| Docker Compose | Docker Compose v2 and a persistent host directory |
| Multi-account | one FastAPI process, account manifest, one password variable per account |
| Deployment verifier | Python 3.12+, `uv`, and HTTP access to the service |

Clone the default branch:

```bash
git clone https://github.com/csxq0605/manyselves.git
cd manyselves
```

Update an existing checkout:

```bash
git switch main
git pull --ff-only origin main
```

## Local React + FastAPI

### Linux/macOS complete startup

Create the local environment file:

```bash
cp .env.example .env
```

Edit `.env`. At minimum, replace the administrator password and configure one provider in the file or later through the Web settings page:

```dotenv
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=replace-with-a-long-random-password
MANYSELVES_ALLOWED_ORIGINS='["http://127.0.0.1:9090"]'
MANYSELVES_HTTP_BIND=127.0.0.1
MANYSELVES_HTTP_PORT=9090

MIMO_API_KEY=
# OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
# DEEPSEEK_API_KEY=
# OPENROUTER_API_KEY=
```

Start:

```bash
chmod +x start.sh
./start.sh
```

The root `start.sh` delegates to `scripts/start.sh`. The script builds the React assets when required, creates `.venv`, installs the Python package, loads `.env`, and starts the FastAPI process.

### Windows complete startup

Create and edit `.env`:

```bat
copy .env.example .env
```

The Windows batch loader should receive the origins value without outer single quotes:

```dotenv
MANYSELVES_ALLOWED_ORIGINS=["http://127.0.0.1:9090"]
```

Build the React client and start the API from the repository root:

```bat
npm --prefix frontend install
npm --prefix frontend run build
scripts\start.bat
```

The Windows entry point is `scripts\start.bat`.

### Addresses

Repository examples use port `9090`:

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

`run_web.py` defaults to port `9000`; set `--port 9090` explicitly when the browser origin or verification commands expect the repository example URL.

Useful arguments:

```text
--host
--port
--workers
--reload
--data-dir
--log-level
--accounts-file
```

## Electron

Electron packages the built React workspace and connects it to the ManySelves FastAPI boundary.

Build the Web assets and Electron main process:

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

Output is written below `desktop/release/`.

## Multi-account mode

Copy and protect the account manifest:

```bash
cp deploy/accounts.example.yaml deploy/accounts.yaml
chmod 600 deploy/accounts.yaml
```

Set every password variable referenced by `passwordEnv` in the manifest:

```bash
export MANYSELVES_ACCOUNT_ADMIN_PASSWORD='yuanxi@2026'
export MANYSELVES_ACCOUNT_YUANXI_001_PASSWORD='yuanxi@2026'
export MANYSELVES_ACCOUNT_YUANXI_002_PASSWORD='yuanxi@2026'
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

Multi-account mode requires `--workers 1`. Each authenticated account is routed to an isolated runtime graph and durable root:

```text
.manyselves/accounts/<account-id>/
```

Provider keys can be configured independently after login. Do not place real account manifests, passwords, or API keys in source control.

The CentOS Podman deployment script enables these three accounts by default. It
creates an independent Xiaomi MiMo Token Plan (China) configuration for each
account without an API Key. Each user enters and saves their own key in the
settings UI. Existing account configuration files are never overwritten. When
upgrading from legacy single-account storage, the original project and provider
configuration are copied to `accounts/admin` while the source data remains in
place.

## Specialized split-process reporting worker

The `manyselves-headless` entry point separates a reporting API process from a worker process. It is a specialized execution mode for the bundled scenario demo, not the normal Web or Compose path.

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

Worker/provider selection uses:

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

Edit `deploy/.env`. The deployment requires an explicit administrator password, a persistent host data directory, and an exact browser origin:

```dotenv
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=replace-with-a-long-random-password
MANYSELVES_DATA_DIR=/srv/manyselves/data

MANYSELVES_HTTP_BIND=127.0.0.1
MANYSELVES_HTTP_PORT=9090
MANYSELVES_ALLOWED_ORIGINS=["http://127.0.0.1:9090"]
```

For a public hostname behind a reverse proxy, use the exact HTTPS origin. For direct network exposure, set an appropriate bind address and protect the service with a firewall and HTTPS.

Create the persistent host directory before starting:

```bash
sudo install -d -m 0750 /srv/manyselves/data
```

The container process must be able to write this directory. The Compose volume uses `:Z` for SELinux-enabled hosts.

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

After changing `deploy/.env`, recreate the affected container so the process receives the new environment:

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

From a source checkout:

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

The verifier checks authentication, live/ready health, runtime availability, projects, conversations, a reversible file round trip, artifact download, the SSE response type, lease cleanup, and logout.

## Backup and restore

### Backup

```bash
set -a
. deploy/.env
set +a

export MANYSELVES_API_URL=http://127.0.0.1:9090
deploy/backup/backup.sh /srv/manyselves/backups
```

Keep backup files outside the active `MANYSELVES_DATA_DIR`.

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

Run the health checks and public-API verifier after restore or rollback.

## Development checks

```bash
uv sync
uv run pytest -q
uv run ruff check manyselves tests scripts
npm --prefix frontend run verify
npm --prefix desktop run verify
```

## Troubleshooting

- **No provider available:** the workspace can load, but Agent calls require an enabled provider with a valid key.
- **Port conflict:** run `lsof -i :9090`, stop the conflicting process, or change the port and allowed origin together.
- **Stale or missing frontend:** remove `frontend/dist`, then run `npm --prefix frontend install` and `npm --prefix frontend run build`.
- **Windows starts without the Web UI:** build `frontend/` before running `scripts\start.bat`.
- **Rejected account manifest:** require `version: 1`, unique IDs and usernames, valid `passwordEnv` values, mode `600` on non-Windows, and `--workers 1`.
- **Environment change not visible:** restart the local process or recreate the container; a browser refresh cannot replace server environment variables.
- **Data permission error:** make the data directory writable by the service account and keep one authoritative write root per account or deployment.
- **Live succeeds but ready fails:** inspect API logs and verify the data directory, account manifest, and runtime/provider configuration.

## Security

- Replace every example password before shared or networked use.
- Keep API keys, active `.env` files, runtime secrets, and account manifests out of source control.
- Bind to loopback by default; use HTTPS and exact allowed origins for networked deployments.
- Restrict the data root and backup directory with operating-system permissions.
- Do not attach unrelated processes or multiple API replicas to the same authoritative runtime write root.
