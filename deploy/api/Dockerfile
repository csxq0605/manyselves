FROM ghcr.io/astral-sh/uv:0.11.6 AS uv
FROM python:3.12-slim AS builder
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY manyselves ./manyselves
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
RUN groupadd --system manyselves && useradd --system --gid manyselves --home-dir /home/manyselves --create-home manyselves \
    && mkdir -p /data/manyselves && chown -R manyselves:manyselves /data/manyselves
WORKDIR /app
COPY --from=builder --chown=manyselves:manyselves /app/.venv /app/.venv
COPY --from=builder --chown=manyselves:manyselves /app/manyselves /app/manyselves
COPY --chown=manyselves:manyselves --chmod=755 deploy/api/entrypoint.sh deploy/api/healthcheck.py deploy/api/init_config.py scripts/init_multi_account_data.py /app/deploy/api/
COPY --chown=manyselves:manyselves deploy/accounts.example.yaml /app/deploy/config/accounts.yaml
COPY --chown=manyselves:manyselves deploy/config/manyselves.account-default.yaml /app/deploy/config/manyselves.account-default.yaml
ENV PATH="/app/.venv/bin:$PATH" DATA_ROOT=/data/manyselves PYTHONUNBUFFERED=1
USER manyselves
EXPOSE 9000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 CMD ["python", "/app/deploy/api/healthcheck.py"]
ENTRYPOINT ["/app/deploy/api/entrypoint.sh"]
