#!/bin/sh
set -eu

DATA_ROOT=${MANYSELVES_DATA_DIR:?set MANYSELVES_DATA_DIR}
OUTPUT_DIR=${1:-./backups}
API_URL=${MANYSELVES_API_URL:-}
ADMIN_USERNAME=${MANYSELVES_ADMIN_USERNAME:-admin}
ADMIN_PASSWORD=${MANYSELVES_ADMIN_PASSWORD:-}
COOKIE_JAR=""
LEASE_TOKEN=""
MAINTENANCE_TOKEN=""

login_json() {
  ADMIN_USERNAME="$ADMIN_USERNAME" ADMIN_PASSWORD="$ADMIN_PASSWORD" \
    python -c 'import json, os; print(json.dumps({"username": os.environ["ADMIN_USERNAME"], "password": os.environ["ADMIN_PASSWORD"]}))'
}

maintenance_json() {
  MAINTENANCE_TOKEN="$MAINTENANCE_TOKEN" \
    python -c 'import json, os; print(json.dumps({"maintenanceToken": os.environ["MAINTENANCE_TOKEN"]}))'
}

lease_json() {
  LEASE_TOKEN="$LEASE_TOKEN" \
    python -c 'import json, os; print(json.dumps({"leaseToken": os.environ["LEASE_TOKEN"]}))'
}

cleanup() {
  trap - EXIT INT TERM
  set +e
  if [ -n "$MAINTENANCE_TOKEN" ]; then
    maintenance_json | curl --fail --silent --show-error --cookie "$COOKIE_JAR" -X POST \
      "$API_URL/api/v1/maintenance/release" \
      -H "X-Control-Lease-Token: $LEASE_TOKEN" -H "Content-Type: application/json" \
      --data-binary @- >/dev/null
  fi
  if [ -n "$LEASE_TOKEN" ]; then
    lease_json | curl --fail --silent --show-error --cookie "$COOKIE_JAR" -X DELETE \
      "$API_URL/api/v1/control/lease" -H "Content-Type: application/json" \
      --data-binary @- >/dev/null
  fi
  if [ -n "$COOKIE_JAR" ]; then
    curl --silent --show-error --cookie "$COOKIE_JAR" -X POST \
      "$API_URL/api/v1/auth/logout" >/dev/null
    rm -f "$COOKIE_JAR"
  fi
}
trap cleanup EXIT INT TERM

if [ -n "$API_URL" ]; then
  [ -n "$ADMIN_PASSWORD" ] || {
    echo "MANYSELVES_ADMIN_PASSWORD is required with MANYSELVES_API_URL" >&2
    exit 2
  }
  COOKIE_JAR=$(mktemp)
  login_json | curl --fail --silent --show-error --cookie-jar "$COOKIE_JAR" -X POST \
    "$API_URL/api/v1/auth/login" -H "Content-Type: application/json" \
    --data-binary @- >/dev/null
  LEASE_TOKEN=$(curl --fail --silent --show-error --cookie "$COOKIE_JAR" -X POST \
    "$API_URL/api/v1/control/lease" -H "Content-Type: application/json" \
    -d '{"clientId":"phase1-backup","actorId":"operator-backup"}' | \
    python -c 'import json,sys; print(json.load(sys.stdin)["leaseToken"])')
  MAINTENANCE_TOKEN=$(curl --fail --silent --show-error --cookie "$COOKIE_JAR" -X POST \
    "$API_URL/api/v1/maintenance/quiesce" -H "X-Control-Lease-Token: $LEASE_TOKEN" | \
    python -c 'import json,sys; print(json.load(sys.stdin)["maintenanceToken"])')
fi

python "$(dirname "$0")/archive.py" backup --source "$DATA_ROOT" --output "$OUTPUT_DIR"
