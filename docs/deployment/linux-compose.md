# Linux Docker/Podman Compose deployment

Phase 1 runs one authoritative Agent Runtime per Compose stack. It is suitable for one trusted enterprise team sharing one active scenario; it is not a public, multi-tenant, or per-user-RBAC deployment.

## Prerequisites and trust boundary

- A 64-bit Linux server on the trusted LAN, such as `192.168.8.28`.
- Docker Compose v2, `podman compose`, or `podman-compose`.
- A dedicated non-root service account and an absolute data directory owned by that account.
- One configured model provider and its API key. The API key is required only when an Agent actually calls that provider.
- Administrator credentials stored only in `deploy/.env`; the reviewed defaults are `admin / yuanxi@2026`.

Only publish TCP `9090` to the trusted LAN. FastAPI/Gunicorn listens on TCP `9000` only inside the Compose network. This IP-only layout has no TLS, so never expose it to the public Internet or an untrusted Wi-Fi/VPN segment.

Create the dedicated `manyselves` account before installation. Run the directory creation commands below as a root-capable administrator; do not make `/opt` or `/srv` writable by a regular login account. All archive extraction, image loading, and Compose commands then run as `manyselves` so containers and persistent files have one non-root owner.

## Option A: upload the source and build on the server

Upload a source archive to `/tmp/manyselves-source.tar.gz`. As an administrator, create the service-owned paths once:

```bash
sudo install -d -m 0750 -o manyselves -g manyselves /opt/manyselves
sudo install -d -m 0700 -o manyselves -g manyselves /srv/manyselves/data /srv/manyselves/backups
sudo chown manyselves:manyselves /tmp/manyselves-source.tar.gz
sudo -u manyselves -H sh -c 'cd /opt/manyselves
tar -xzf /tmp/manyselves-source.tar.gz
cp deploy/env.example deploy/.env
chmod 0600 deploy/.env'
```

Edit `/opt/manyselves/deploy/.env` as `manyselves`. Keep these reviewed LAN values unless the server address or port changes:

```dotenv
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=yuanxi@2026
MANYSELVES_ALLOWED_ORIGINS=["http://192.168.8.28:9090"]
MANYSELVES_DATA_DIR=/srv/manyselves/data
MANYSELVES_HTTP_BIND=0.0.0.0
MANYSELVES_HTTP_PORT=9090
MANYSELVES_INITIAL_PROJECT_ID=default
```

Set `MANYSELVES_BOOTSTRAP_PROVIDER` and the matching provider key. The entrypoint creates a provider skeleton only when `manyselves.config.yaml` is absent; it does not write the environment key into that YAML or replace an existing configuration.

Docker commands:

```bash
sudo -u manyselves -H sh -c 'cd /opt/manyselves && docker compose -f deploy/compose.yaml --env-file deploy/.env config'
sudo -u manyselves -H sh -c 'cd /opt/manyselves && docker compose -f deploy/compose.yaml --env-file deploy/.env build'
sudo -u manyselves -H sh -c 'cd /opt/manyselves && docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --wait'
```

Podman commands:

```bash
sudo -u manyselves -H sh -c 'cd /opt/manyselves && podman compose -f deploy/compose.yaml --env-file deploy/.env config'
sudo -u manyselves -H sh -c 'cd /opt/manyselves && podman compose -f deploy/compose.yaml --env-file deploy/.env build'
sudo -u manyselves -H sh -c 'cd /opt/manyselves && podman compose -f deploy/compose.yaml --env-file deploy/.env up -d'
```

If the installed Compose provider is the standalone command, replace `podman compose` with `podman-compose`. Some Podman Compose versions do not support `--wait`; use `podman ps` and the health checks below instead. Do not increase API workers or replicas, because that would create multiple process-local runtimes.

## Option B: transfer prebuilt Linux images

Transfer Linux container images and the release source archive, not the whole WSL distribution. Confirm the server architecture with `uname -m`; the example below is for `x86_64`/`linux/amd64`.

On the build computer:

```bash
docker save --output manyselves-phase1-linux-amd64.tar \
  manyselves-api:phase1 manyselves-web:phase1
git archive --format=tar.gz --output manyselves-phase1-release.tar.gz HEAD
sha256sum manyselves-phase1-linux-amd64.tar manyselves-phase1-release.tar.gz \
  > manyselves-phase1-transfer.sha256
```

Upload the three files to the server. On the server:

```bash
sudo install -d -m 0750 -o manyselves -g manyselves /opt/manyselves
sudo install -d -m 0700 -o manyselves -g manyselves /srv/manyselves/data /srv/manyselves/backups
sudo chown manyselves:manyselves /tmp/manyselves-phase1-linux-amd64.tar /tmp/manyselves-phase1-release.tar.gz /tmp/manyselves-phase1-transfer.sha256
sudo -u manyselves -H sh -c 'cd /tmp
sha256sum --check manyselves-phase1-transfer.sha256
tar -xzf manyselves-phase1-release.tar.gz -C /opt/manyselves
podman load --input manyselves-phase1-linux-amd64.tar
cd /opt/manyselves
cp deploy/env.example deploy/.env
chmod 0600 deploy/.env'
```

Edit `deploy/.env` as described above. If the loaded image names include `localhost/`, set `MANYSELVES_API_IMAGE` and `MANYSELVES_WEB_IMAGE` in that file to the exact names shown by `podman images`, then start without rebuilding:

```bash
sudo -u manyselves -H sh -c 'cd /opt/manyselves && podman compose -f deploy/compose.yaml --env-file deploy/.env up -d --no-build'
```

## Health, browser access, and uploads

```bash
curl --fail http://127.0.0.1:9090/api/v1/health/live
curl --fail http://127.0.0.1:9090/api/v1/health/ready
```

From a browser computer on the trusted LAN, open `http://192.168.8.28:9090`. The unauthenticated homepage is the login page. Sign in with the administrator username and password from `deploy/.env`.

Every upload action selects files from the computer running that browser. The browser sends their bytes to the API and the authoritative copy remains under the server data directory. The UI does not expose a general server-file browser and does not synchronize a workstation directory.

All signed-in users can observe the shared state. Exactly one client holds the mutation lease at a time; after an abnormal client loss, wait for the configured lease TTL before another client takes control.

## Logs and routine operation

Docker users can replace `podman compose` below with `docker compose`:

```bash
podman compose -f deploy/compose.yaml --env-file deploy/.env ps
podman compose -f deploy/compose.yaml --env-file deploy/.env logs -f --tail 200 api web
podman compose -f deploy/compose.yaml --env-file deploy/.env restart
podman compose -f deploy/compose.yaml --env-file deploy/.env stop
podman compose -f deploy/compose.yaml --env-file deploy/.env start
```

Nginx serves port `9090`, proxies to `api:9000`, forwards browser cookies and origins normally, disables SSE buffering, serves hashed assets immutably, and keeps `index.html` uncached. Keep `MANYSELVES_ALLOWED_ORIGINS` equal to the exact browser origin, never `*`.

After installation, restart, restore, or upgrade, run the public verifier from the release source directory:

```bash
set -a
. deploy/.env
set +a
uv run python scripts/verify_deployment.py \
  --url http://192.168.8.28:9090 \
  --username "$MANYSELVES_ADMIN_USERNAME" \
  --password-env MANYSELVES_ADMIN_PASSWORD
```

The verifier logs in once, checks health, the single Runtime, projects, conversations, a reversible file round trip, SSE, and an artifact download; it then removes its temporary file, releases the control lease, and logs out.

## Multiple active scenarios

One stack owns one active Runtime and one data directory. For scenarios that must run concurrently, use a unique Compose project name, host port, allowed origin, credentials, and data directory for each stack. Never attach two API containers to the same data directory.

## Backup and restore

For an online-consistent backup, load the server-only credentials from `deploy/.env`, set the API URL, and run the backup script. It logs in, acquires the controller lease, enters maintenance, creates the archive, then releases maintenance and the lease and logs out in its cleanup path.

```bash
set -a
. deploy/.env
set +a
export MANYSELVES_API_URL=http://192.168.8.28:9090
deploy/backup/backup.sh /srv/manyselves/backups
```

Restore is offline and guarded. It verifies paths and hashes, rejects unsafe archive members, and preserves the previous target as a timestamped sibling when `--force` is used.

```bash
podman compose -f deploy/compose.yaml --env-file deploy/.env down
export MANYSELVES_SERVICES_STOPPED=yes
deploy/backup/restore.sh /srv/manyselves/backups/manyselves-YYYYMMDDTHHMMSSZ.tar.gz --force
podman compose -f deploy/compose.yaml --env-file deploy/.env up -d
```

## Upgrade and rollback

Back up first and record the current image names and digests. Recreate the stack with the new immutable images against the same data directory. To roll back, stop the stack, select the previous image tags/digests, restore only if the upgrade changed authoritative data, start the previous stack, and run the public verifier again.
