# Linux Docker Compose deployment

Phase 1 runs one authoritative Agent Runtime per Compose stack. It is suitable for one enterprise team sharing one active scenario; run a separate stack and data directory for each concurrently active scenario. It is not a multi-tenant or per-user-RBAC deployment.

## Prerequisites and trust boundary

- 64-bit Linux, Docker Engine 27+ and Docker Compose v2.
- A dedicated non-root service account and the trusted `192.168.8.0/24` enterprise LAN.
- One provider API key and one independently generated deployment access token.
- A dedicated absolute data directory owned by the service account. This directory is the authoritative store; container layers are disposable.

The API permits server-side Python and Agent tools. Do not expose this deployment to the public Internet. Phase 1 uses direct HTTP only inside the trusted LAN, one deployment token, and one controller lease; it does not provide user accounts or tenant isolation.

## Install and start

```bash
install -d -m 0700 /srv/manyselves/data /srv/manyselves/backups
cp deploy/env.example deploy/.env
chmod 0600 deploy/.env
```

Edit `deploy/.env`: set an absolute `MANYSELVES_DATA_DIR`, a random `MANYSELVES_ACCESS_TOKEN`, the matching `MANYSELVES_BOOTSTRAP_PROVIDER`, and that provider's key. Keep the reviewed defaults `MANYSELVES_HTTP_BIND=0.0.0.0`, `MANYSELVES_HTTP_PORT=9090`, and `MANYSELVES_ALLOWED_ORIGINS=["http://192.168.8.28:9090"]`. The entrypoint creates a provider skeleton only when `manyselves.config.yaml` is absent; it never writes the environment key into that skeleton or replaces an existing file.

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env config
docker compose -f deploy/compose.yaml --env-file deploy/.env build
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --wait
curl --fail http://127.0.0.1:9090/api/v1/health/live
curl --fail http://127.0.0.1:9090/api/v1/health/ready
```

The API is deliberately fixed to one Gunicorn worker and Compose replica. Increasing either creates multiple process-local runtimes and is unsupported.

## Transfer prebuilt images from WSL or Docker Desktop

Transfer the Linux Docker images, not the complete WSL distribution. The reviewed images are `linux/amd64`; confirm the server reports `x86_64` from `uname -m`. An ARM server requires separately built `linux/arm64` images.

From WSL at the reviewed repository commit:

```bash
docker save --output manyselves-phase1-linux-amd64.tar \
  manyselves-api:phase1 manyselves-web:phase1
git archive --format=tar.gz --output manyselves-phase1-release.tar.gz HEAD
sha256sum manyselves-phase1-linux-amd64.tar manyselves-phase1-release.tar.gz \
  > manyselves-phase1-transfer.sha256
scp manyselves-phase1-linux-amd64.tar manyselves-phase1-release.tar.gz \
  manyselves-phase1-transfer.sha256 USER@192.168.8.28:/tmp/
```

On the Linux server:

```bash
cd /tmp
sha256sum --check manyselves-phase1-transfer.sha256
sudo install -d -o MANYSELVES_USER -g MANYSELVES_USER -m 0750 /opt/manyselves
sudo -u MANYSELVES_USER tar -xzf manyselves-phase1-release.tar.gz -C /opt/manyselves
docker load --input manyselves-phase1-linux-amd64.tar
cd /opt/manyselves
cp deploy/env.example deploy/.env
chmod 0600 deploy/.env
```

Replace `USER` and `MANYSELVES_USER`, then edit `deploy/.env` as described above. Start the loaded immutable tags without rebuilding:

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env config
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --no-build --wait
curl --fail http://127.0.0.1:9090/api/v1/health/ready
```

Do not use `wsl --export` for this Linux-server workflow. It moves an entire Windows WSL filesystem and configuration, is much larger, and is not the deployable artifact consumed by Docker Engine on the server.

## LAN firewall and ports

The only host-published port is TCP `9090`, served by the stack's Nginx container. Allow it only from the trusted enterprise LAN, for example `192.168.8.0/24`. FastAPI/Gunicorn listens on TCP `9000` inside the Compose network and must not be added to `ports`, the host firewall, or the cloud security group.

Users connect directly to `http://192.168.8.28:9090`; no domain name is required. Because Phase 1 has no TLS in this layout, never expose TCP `9090` to the public Internet or an untrusted Wi-Fi/VPN segment. If public or cross-network access is needed later, add an approved TLS reverse proxy and change the exact allowed origin as a separate deployment change.

## User access modes

1. **Browser (recommended):** users open `http://192.168.8.28:9090`. In “服务器连接”, enter that same URL and the deployment token. Browser token state is session-scoped, so a new browser profile may need the token again.
2. **Electron (optional):** install the packaged client, confirm `http://192.168.8.28:9090`, and enter the deployment token. The token is stored with Electron `safeStorage` on that device; project files remain on the server.
3. **PyQt fallback:** run `uv run manyselves` on an operator workstation for legacy/local workflows. It is not the server process and should not point multiple desktop processes at one live server data directory.

All connected users can observe state. Exactly one client holds the mutation lease at a time. Normal UI actions acquire/renew it; after an abnormal client loss, wait for the configured lease TTL (default 30 seconds) before another client takes control.

## Multiple enterprise scenarios

One stack supports one active scenario at a time. For concurrent scenarios, use a unique Compose project name, host port, deployment token, allowed origin, and data directory for every stack:

```bash
docker compose -p manyselves-energy \
  -f deploy/compose.yaml --env-file deploy/energy.env up -d --wait
docker compose -p manyselves-audit \
  -f deploy/compose.yaml --env-file deploy/audit.env up -d --wait
```

For example, `energy.env` can use port 9091, origin `http://192.168.8.28:9091`, and `/srv/manyselves/energy/data`, while `audit.env` uses port/origin 9092 and `/srv/manyselves/audit/data`. Never attach two API containers or two stacks to the same data directory.

## Logs and routine operation

Nginx inside the stack proxies to `api:9000`, preserves the required authentication/control headers, disables SSE buffering, serves hashed assets immutably, and keeps `index.html` uncached. `MANYSELVES_ALLOWED_ORIGINS` must remain the exact browser origin, never `*`.

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env ps
docker compose -f deploy/compose.yaml --env-file deploy/.env logs -f --tail 200 api web
docker compose -f deploy/compose.yaml --env-file deploy/.env restart
docker compose -f deploy/compose.yaml --env-file deploy/.env stop
docker compose -f deploy/compose.yaml --env-file deploy/.env start
```

After installation, restart, restore, or upgrade, run the public verifier:

```bash
set -a
. deploy/.env
set +a
uv run python scripts/verify_deployment.py \
  --url http://192.168.8.28:9090 \
  --token-env MANYSELVES_ACCESS_TOKEN
```

The verifier creates one temporary server file, reads/downloads it, verifies SSE, removes the file, and releases its control lease.

## Backup and restore

For an online-consistent backup, set `MANYSELVES_API_URL` to the LAN URL in addition to the data directory and token. The backup script acquires a controller lease, enters maintenance, creates a SHA-256 manifest archive, and releases maintenance in a trap/finally block.

```bash
export MANYSELVES_DATA_DIR=/srv/manyselves/data
export MANYSELVES_API_URL=http://192.168.8.28:9090
export MANYSELVES_ACCESS_TOKEN='...'
deploy/backup/backup.sh /srv/manyselves/backups
```

Restore is offline and guarded. It verifies all paths and hashes, rejects symbolic links and non-empty targets by default, and with `--force` preserves the previous target as a timestamped sibling.

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env down
export MANYSELVES_SERVICES_STOPPED=yes
deploy/backup/restore.sh /srv/manyselves/backups/manyselves-YYYYMMDDTHHMMSSZ.tar.gz --force
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --wait
```

## Upgrade and rollback

Back up first, record the current image digests, build or pull the new version, then recreate the stack against the same data directory. For rollback, stop the stack, select the previous immutable image tags/digests, restore only if the upgrade changed authoritative data, and start with `--wait`. Phase 1 persistence remains the existing workspace/config format and requires no one-way migration.
