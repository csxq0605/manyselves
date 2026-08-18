# Running ManySelves

This is the operational reference for local, desktop, and container startup. Design history, phase plans, handoff notes, and debug journals are intentionally excluded.

## Requirements

| Mode | Requirements |
| --- | --- |
| PyQt desktop | Python 3.12+, `uv`, model-provider key |
| React + FastAPI | Python 3.12+, Node.js 22+, npm, model-provider key |
| Electron | React build plus `desktop/` dependencies |
| Compose | Docker Compose v2 and a persistent host directory |
| Multi-account | One FastAPI process, account manifest, one password environment variable per account |

Clone the maintained branch:

```bash
git clone --branch feature/react-fastapi-manyselves \
  https://github.com/csxq0605/manyselves.git
cd manyselves
```

## PyQt desktop

```bash
uv sync
cp manyselves.config.example.yaml manyselves.config.yaml
uv run manyselves
```

The application can start without a provider key, but model calls require an enabled provider. Configure one in the UI or through supported environment variables.

## React + FastAPI

### Complete local startup

Linux/macOS:

```bash
cp .env.example .env
# Replace example credentials and provider settings.
chmod +x start.sh
./start.sh
```

Windows:

```bat
copy .env.example .env
start.bat
```

The startup script builds `frontend/` when needed, prepares a Python virtual environment, and starts the API. Repository examples use port `9090`.

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

`run_web.py` itself defaults to port `9000`; set the port explicitly when you need one stable URL.

Useful arguments:

```text
--host  --port  --workers  --reload
--data-dir  --log-level  --accounts-file
```

## Electron

Build the React assets, then the Electron main process:

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

The Electron client expects a reachable ManySelves FastAPI service.

## Split-process headless reporting

This specialized entry point separates a reporting API process from a worker process. It is not required for the normal PyQt or FastAPI paths.

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

Provider type, base URL, model, secret variable name, and worker ID can be selected with `MANYSELVES_PROVIDER_TYPE`, `MANYSELVES_PROVIDER_API_BASE`, `MANYSELVES_PROVIDER_MODEL`, `MANYSELVES_PROVIDER_API_KEY_SECRET`, and `MANYSELVES_WORKER_ID`.

## Multi-account mode

```bash
cp deploy/accounts.example.yaml deplloy/accounts.yaml
chmod 600 deploy/accounts.yaml

export MANYSELVES_ACCOUNT_A_PASSWORD='replace-me'
export MANYSELVES_ACCOUNT_B_PASSWORD='replace-me'

uv run python run_web.py \
  --accounts-file deploy/accounts.yaml \
  --workers 1 \
  --host 127.0.0.1 \
  --port 9090 \
  --data-dir .manyselves
```

Multi-account mode requires one process (`--workers 1`). Each account runtime is created lazily and writes below `.manyselves/accounts/<account-id>/`.

## Docker Compose

```bash
cp deploy/env.example deploy/.env
chmod 600 deploy/.env
# Replace the password, host data directory, and exact browser origin.

docker compose -f deploy/compose.yaml --env-file deploy/.env config
docker compose -f deploy/compose.yaml --env-file deploy/.env build
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --wait
```

Operations:

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env ps
docker compose -f deploy/compose.yaml --env-file deploy/.env logs -f --tail 200 api web
docker compose -f deploy/compose.yaml --env-file deploy/.env restart
```

After changing `deploy/.env`, recreate the affected container so it receives the new environment:

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env \
  up -d --force-recreate api
```

## Verify a deployment

```bash
curl --fail http://127.0.0.1:9090/api/v1/health/live
curl --fail http://127.0.0.1:9090/api/v1/health/ready
```

Public-API verifier:

```bash
set -a
.
deploy/.env
set +a

uv run python scripts/verify_deployment.py \
  --url http://127.0.0.1:9090 \
  --username "$MANYSELVES_ADMIN_USERNAME" \
  --password-env MANYSELVES_ADMIN_PASSWORD
```

It checks authentication, health, runtime availability, projects, conversations, a reversible file operation, event streaming, and artifact download.

## Backup and restore

Backup:

```bash
set -a
. deploy/.env
set +a
export MANYSELVES_API_URL=http://127.0.0.1:9090

deploy/backup/backup.sh /srv/manyselves/backups
```

Restore is offline:

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env down
export MANYSELVES_SERVICES_STOPPED=yes

deploy/backup/restore.sh \
  /srv/manyselves/backups/manyselves-YYYYMMDDTHHMMSSZ.tar.gz \
  --force

docker compose -f deploy/compose.yaml --env-file deploy/.env up -d
```

Keep backups outside the active data root and verify the deployment after restore or rollback.

## Development checks

```bash
uv sync
QT_QPA_PLATFORM=offscreen uv run pytest -q
uv run ruff check manyselves tests scripts
npm --prefix frontend run verify
npm --prefix desktop run verify
```

## Troubleshooting

- **No provider available:** the service may start in degraded mode, but Agent calls require an enabled provider and valid key.
- **Port conflict:** run `lsof -i :9090` or select another `--port`.
- **Stale frontend:** remove `frontend/dist`, run `npm --prefix frontend install`, then `npm --prefix frontend run build`.
- **Rejected account manifest:** require `version: 1`, unique IDs/usernames, valid `passwordEnv` values, mode `600` on non-Windows, and `--workers 1`.
- **Environment change not visible:** restart the process or recreate the container; a browser refresh cannot replace process environment variables.
- **Data permission error:** make the data directory writable by the dedicated service account and keep one write root per account or stack.

## Security

- Replace every example password before shared or networked use.
- Keep API keys, active `.env` files, and account manifests out of source control.
- Use HTTPS on any untrusted network and configure the exact allowed browser origin.
- Do not attach multiple API replicas or processes to the same runtime data root.
