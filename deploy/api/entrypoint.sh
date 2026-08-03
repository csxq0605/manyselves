#!/bin/sh
set -eu

python /app/deploy/api/init_config.py

exec gunicorn manyselves.webapi.main:app \
  --workers 1 --worker-class uvicorn_worker.UvicornWorker \
  --bind 0.0.0.0:9000 --access-logfile - --error-logfile - \
  --graceful-timeout 55 --timeout 120 --no-control-socket
