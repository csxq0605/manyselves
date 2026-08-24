#!/bin/sh
set -eu

if [ -n "${MANYSELVES_ACCOUNTS_FILE:-}" ]; then
  python /app/deploy/api/init_multi_account_data.py \
    --data-root "${DATA_ROOT:-/data/manyselves}" \
    --accounts-file "${MANYSELVES_ACCOUNTS_FILE}" \
    --accounts-template /app/deploy/config/accounts.yaml \
    --config-template /app/deploy/config/manyselves.account-default.yaml
else
  python /app/deploy/api/init_config.py
fi

exec gunicorn manyselves.webapi.main:app \
  --workers 1 --worker-class uvicorn_worker.UvicornWorker \
  --bind 0.0.0.0:9000 --access-logfile - --error-logfile - \
  --graceful-timeout 55 --timeout 120 --no-control-socket
