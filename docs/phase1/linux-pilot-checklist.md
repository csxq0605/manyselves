# Phase 1 Linux pilot checklist

## Automated local Linux-container drill (2026-08-03)

- [x] Docker Engine 28.5.1 / Compose 2.40.2 build both images.
- [x] API and web containers become healthy.
- [x] API runs as UID 999 with one Gunicorn master and exactly one worker.
- [x] API port `9000` is not published directly; the local drill overrides the web bind to `127.0.0.1:9090`.
- [x] Deployment verifier passes live, ready, single runtime, project, conversation, file round trip, SSE, and artifact download.
- [x] Verifier removes its temporary file and releases the controller lease.
- [x] Online backup, manifest validation, restore-to-new-directory, and post-backup readiness were exercised.

This drill used Docker Desktop's Linux engine. It is strong packaging evidence but is not a substitute for the target enterprise Linux/TLS/network pilot.

## Target Linux production pilot

- [ ] Record distribution, kernel, Docker/Compose versions, UTC start/end, operator, and reviewer.
- [ ] Create a dedicated non-root account and `0700` data/backup directories.
- [ ] Restrict TCP `9090` to the trusted `192.168.8.0/24` LAN; do not publish TCP `9000`.
- [ ] Confirm `MANYSELVES_ALLOWED_ORIGINS` is exactly `["http://192.168.8.28:9090"]`.
- [ ] Build or pull immutable image digests; run the Trivy command below.
- [ ] Start Compose with `up -d --wait`; verify exactly one API worker.
- [ ] Run `scripts/verify_deployment.py` against `http://192.168.8.28:9090`.
- [ ] Complete one browser and one packaged Electron smoke flow.
- [ ] Complete one deterministic Agent flow and one Reporting resume/download flow.
- [ ] Restart containers and re-run the verifier to prove persistence.
- [ ] Run online backup, stop, restore into an empty data directory, start, and re-run the verifier.
- [ ] Upgrade to a second immutable tag and roll back to the recorded prior digest.
- [ ] Record reviewer sign-off and update the parity matrix row by row.

```bash
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  aquasec/trivy:latest image --severity HIGH,CRITICAL --ignore-unfixed \
  --exit-code 1 manyselves-api:phase1

export MANYSELVES_ADMIN_USERNAME=admin
export MANYSELVES_ADMIN_PASSWORD='change-this-before-production'
uv run python scripts/verify_deployment.py \
  --url http://192.168.8.28:9090 \
  --username "$MANYSELVES_ADMIN_USERNAME" \
  --password-env MANYSELVES_ADMIN_PASSWORD
```
