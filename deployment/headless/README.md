# Headless deployment contract

Run API and workers as separate processes against the same project-isolated POSIX volume and service-state volume.

Required paths:

- `MANYSELVES_PROJECT_STORAGE_ROOT`
- `MANYSELVES_SERVICE_STATE_ROOT`
- `MANYSELVES_CONFIG_ROOT`

Workers additionally require `MANYSELVES_PROVIDER_TYPE`, `MANYSELVES_PROVIDER_API_KEY` (or the allowlisted name in `MANYSELVES_PROVIDER_API_KEY_SECRET`), and optional Provider base/model values. The API process never loads the Provider secret. Build and deployment automation must record the image digest, source commit, build epoch, migration version, data/model regions, retention, RPO, and RTO in the deployment identity record.

The Docker build deliberately has no floating default base image. Supply an immutable digest, for example `--build-arg BASE_IMAGE=python@sha256:<approved-digest>`.
