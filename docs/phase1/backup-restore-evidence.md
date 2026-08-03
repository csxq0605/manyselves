# Phase 1 backup and restore evidence

Local drill date: 2026-08-03 (Asia/Shanghai). Environment: Docker Desktop Linux engine.

- Online backup archive: `manyselves-20260803T070606Z.tar.gz`
- Size: 1,512 bytes (small deterministic fixture workspace)
- SHA-256: `CE7E0D61280F1477123D5DB3248DF7D99F22E020B365389157A2B7178E548B7B`
- The backup acquired the control lease, quiesced maintenance, wrote a manifest-hashed archive, and released maintenance in `finally`/shell trap handling.
- `/api/v1/health/ready` returned ready after backup, proving maintenance was released.
- Restore to `deploy/restore-smoke` succeeded after path, link, and SHA-256 validation.
- The restore command refuses non-empty targets unless `--force`; forced restore preserves the prior target as a timestamped sibling.

Target-host evidence is intentionally blank until the production pilot records the target archive name, digest, restored data directory, image digests, operator, reviewer, and timestamps.
