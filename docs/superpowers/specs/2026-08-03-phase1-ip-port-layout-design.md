# Phase 1 IP and Port Layout Design

## Goal

Deploy the existing single-runtime Phase 1 stack without a domain name. Clients on the trusted LAN access `http://192.168.8.28:9090`; the API remains private to the Compose network.

## Network contract

```text
LAN browser or Electron
    -> http://192.168.8.28:9090
    -> web container, Nginx :9090
    -> api container, Gunicorn/Uvicorn :9000
```

- Gunicorn binds `0.0.0.0:9000` inside the API container.
- Compose exposes API port `9000` only to the internal network and never publishes it on the host.
- Nginx listens on container port `9090` and proxies `/api/` and SSE to `http://api:9000`.
- Compose publishes `${MANYSELVES_HTTP_BIND}:${MANYSELVES_HTTP_PORT}:9090` with defaults `0.0.0.0:9090` for direct LAN access.
- Default CORS origin is exactly `http://192.168.8.28:9090`; wildcard origins remain forbidden.
- The deployment token remains mandatory for mutations. HTTP without TLS is limited to the trusted `192.168.8.0/24` LAN and must not be exposed to the public Internet.

## Files and compatibility

Update the API entrypoint, API health check, Compose service ports/default origin, Nginx listener/upstream, environment example, deployment contract tests, deployment verifier examples, and operator documentation. Application APIs, persistence, React behavior, Electron IPC, PyQt fallback, one-worker rule, and project data format do not change.

Existing operators with a custom `.env` must change their browser origin and port explicitly. No data migration is required.

## Verification

Use test-first deployment contracts to require:

- Gunicorn `:9000` and health check `:9000`;
- Compose internal API `9000`, web target `9090`, host default `9090`;
- Nginx listener `9090` and both upstreams `api:9000`;
- environment defaults for `192.168.8.28:9090` and LAN bind;
- no host publication of API port `9000`.

Then run deployment tests, Compose config rendering, Docker image builds, `up -d --wait`, health endpoints through `http://127.0.0.1:9090`, and the public deployment verifier. The temporary stack uses fake test credentials and is removed after verification.
