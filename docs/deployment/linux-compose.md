# Linux Docker Compose deployment

Phase 1 runs one authoritative Agent Runtime per Compose stack. It is suitable for one enterprise team sharing one active scenario; run a separate stack and data directory for each concurrently active scenario. It is not a multi-tenant or per-user-RBAC deployment.

## Prerequisites and trust boundary

- 64-bit Linux, Docker Engine 27+ and Docker Compose v2.
- A dedicated non-root service account and a private, TLS-terminated enterprise network.
- One provider API key and one independently generated deployment access token.
- A dedicated absolute data directory owned by the service account. This directory is the authoritative store; container layers are disposable.

The API permits server-side Python and Agent tools. Do not expose it directly to the public Internet. Terminate TLS at an enterprise reverse proxy and restrict source networks. Phase 1 uses one deployment token plus one controller lease, not user accounts or tenant isolation.

## Install and start

```bash
install -d -m 0700 /srv/manyselves/data /srv/manyselves/backups
cp deploy/env.example deploy/.env
chmod 0600 deploy/.env
```

Edit `deploy/.env`: set an absolute `MANYSELVES_DATA_DIR`, a random `MANYSELVES_ACCESS_TOKEN`, the matching `MANYSELVES_BOOTSTRAP_PROVIDER`, and that provider's key. The entrypoint creates a provider skeleton only when `manyselves.config.yaml` is absent; it never writes the environment key into that skeleton or replaces an existing file.

```bash
docker compose -f deploy/compose.yaml --env-file deploy/.env config
docker compose -f deploy/compose.yaml --env-file deploy/.env build
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d --wait
curl --fail http://127.0.0.1:8080/api/v1/health/live
curl --fail http://127.0.0.1:8080/api/v1/health/ready
```

The API is deliberately fixed to one Gunicorn worker and Compose replica. Increasing either creates multiple process-local runtimes and is unsupported.

## Cloud firewall and HTTPS

For an Internet-connected VM, allow inbound SSH only from the administrator network and HTTPS 443 from the enterprise network. Do not open 8000 or 8080 in the cloud security group. Keep `MANYSELVES_HTTP_BIND=127.0.0.1` and place the organization's Caddy, Nginx, ingress, or load balancer in front of it.

A minimal external Nginx location is:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

Use the organization's normal certificate mechanism. Set `MANYSELVES_ALLOWED_ORIGINS` to the exact public origin, for example `["https://manyselves.example.internal"]`; do not use `*`.

## User access modes

1. **Browser (recommended):** users open the HTTPS URL. In “服务器连接”, enter that same URL and the deployment token. Browser token state is session-scoped, so a new browser profile may need the token again.
2. **Electron (optional):** install the packaged client, enter the HTTPS server URL and deployment token. The token is stored with Electron `safeStorage` on that device; project files remain on the server.
3. **PyQt fallback:** run `uv run manyselves` on an operator workstation for legacy/local workflows. It is not the server process and should not point multiple desktop processes at one live server data directory.

All connected users can observe state. Exactly one client holds the mutation lease at a time. Normal UI actions acquire/renew it; after an abnormal client loss, wait for the configured lease TTL (default 30 seconds) before another client takes control.

## Multiple enterprise scenarios

One stack supports one active scenario at a time. For concurrent scenarios, use a unique Compose project name, HTTPS hostname/route, loopback port, deployment token, and data directory for every stack:

```bash
docker compose -p manyselves-energy \
  -f deploy/compose.yaml --env-file deploy/energy.env up -d --wait
docker compose -p manyselves-audit \
  -f deploy/compose.yaml --env-file deploy/audit.env up -d --wait
```

For example, `energy.env` can use port 8081 and `/srv/manyselves/energy/data`, while `audit.env` uses port 8082 and `/srv/manyselves/audit/data`. Never attach two API containers or two stacks to the same data directory.

## TLS, logs, and routine operation

Proxy HTTPS to `127.0.0.1:8080`, preserve `Authorization`, `X-Control-Lease-Token`, `Last-Event-ID`, and `X-Request-ID`, and disable buffering for `/api/v1/events`. Set `MANYSELVES_ALLOWED_ORIGINS` to the exact browser origin. Nginx inside the stack already disables SSE buffering and serves hashed assets immutably while keeping `index.html` uncached.

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
  --url https://manyselves.example.internal \
  --token-env MANYSELVES_ACCESS_TOKEN
```

The verifier creates one temporary server file, reads/downloads it, verifies SSE, removes the file, and releases its control lease.

## Backup and restore

For an online-consistent backup, set `MANYSELVES_API_URL` to the internal HTTPS URL in addition to the data directory and token. The backup script acquires a controller lease, enters maintenance, creates a SHA-256 manifest archive, and releases maintenance in a trap/finally block.

```bash
export MANYSELVES_DATA_DIR=/srv/manyselves/data
export MANYSELVES_API_URL=https://manyselves.example.internal
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
