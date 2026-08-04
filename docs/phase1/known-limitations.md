# Phase 1 known limitations

- One authoritative Runtime and one active project per Compose stack.
- One controller lease at a time; other connected clients are observers until the lease expires or is released.
- One shared administrator login, not per-user authentication, RBAC, tenant isolation, or audit identity.
- Server filesystem is authoritative. Phase 1 has no offline local-project synchronization or conflict merge.
- Agent tools and Python execution require a trusted private network and trusted project users; they are not an untrusted-code sandbox.
- Run a separate stack, port, administrator credentials, and data directory for each concurrently active enterprise scenario.
- Browser and Electron clients use administrator session cookies; this remains a shared-team deployment rather than per-user RBAC.
- TLS is supplied by the enterprise reverse proxy, not the included Compose web container.
- MySQL, Redis, Milvus, MinIO, multi-tenant knowledge bases, user administration, and horizontal Runtime scaling belong to Phase 2 and are deliberately absent from Phase 1.
- Functional tests are green, but the repository-wide Ruff command exposes 127 pre-existing legacy GUI/Reporting lint findings. New Phase 1 release code is Ruff-clean; legacy lint cleanup is deferred to avoid changing validated core behavior during release closure.
