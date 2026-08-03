# Phase 1 Linux pilot checklist

## Automated local Linux-container drill (2026-08-03)

- [x] Docker Engine 28.5.1 / Compose 2.40.2 build both images.
- [x] API and web containers become healthy.
- [x] API runs as UID 999 with one Gunicorn master and exactly one worker.
- [x] API is not published directly; web binds `127.0.0.1:8080`.
- [x] Deployment verifier passes live, ready, single runtime, project, conversation, file round trip, SSE, and artifact download.
- [x] Verifier removes its temporary file and releases the controller lease.
- [x] Online backup, manifest validation, restore-to-new-directory, and post-backup readiness were exercised.

This drill used Docker Desktop's Linux engine. It is strong packaging evidence but is not a substitute for the target enterprise Linux/TLS/network pilot.

## Target Linux production pilot

- [ ] Record distribution, kernel, Docker/Compose versions, UTC start/end, operator, and reviewer.
- [ ] Create a dedicated non-root account and `0700` data/backup directories.
- [ ] Configure an enterprise TLS reverse proxy and private source-network restrictions.
- [ ] Set the exact HTTPS origin in `MANYSELVES_ALLOWED_ORIGINS`.
- [ ] Build or pull immutable image digests; run the Trivy command below.
- [ ] Start Compose with `up -d --wait`; verify exactly one API worker.
- [ ] Run `scripts/verify_deployment.py` against the HTTPS URL.
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

export MANYSLEVES_ACCESS_TOKEN='your-deployment-token'
uv run python scripts/verify_deployment.py \
  --url https://manyselves.example.internal \
  --token-env MANYSLEVES_ACCESS_TOKEN
```
