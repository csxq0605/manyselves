#!/bin/sh
set -eu

DATA_ROOT=${MANYSELVES_DATA_DIR:?set MANYSELVES_DATA_DIR}
OUTPUT_DIR=${1:-./backups}
API_URL=${MANYSELVES_API_URL:-}
ACCESS_TOKEN=${MANYSELVES_ACCESS_TOKEN:-}
LEASE_TOKEN=""
MAINTENANCE_TOKEN=""

release_maintenance() {
  if [ -n "$MAINTENANCE_TOKEN" ]; then
    curl --fail --silent --show-error -X POST "$API_URL/api/v1/maintenance/release" \
      -H "Authorization: Bearer $ACCESS_TOKEN" -H "X-Control-Lease-Token: $LEASE_TOKEN" \
      -H "Content-Type: application/json" -d "{\"maintenanceToken\":\"$MAINTENANCE_TOKEN\"}" >/dev/null
  fi
}
trap release_maintenance EXIT INT TERM

if [ -n "$API_URL" ]; then
  [ -n "$ACCESS_TOKEN" ] || { echo "MANYSELVES_ACCESS_TOKEN is required with MANYSELVES_API_URL" >&2; exit 2; }
  LEASE_TOKEN=$(curl --fail --silent --show-error -X POST "$API_URL/api/v1/control/lease" \
    -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
    -d '{"clientId":"phase1-backup","actorId":"operator-backup"}' | python -c 'import json,sys; print(json.load(sys.stdin)["leaseToken"])')
  MAINTENANCE_TOKEN=$(curl --fail --silent --show-error -X POST "$API_URL/api/v1/maintenance/quiesce" \
    -H "Authorization: Bearer $ACCESS_TOKEN" -H "X-Control-Lease-Token: $LEASE_TOKEN" | \
    python -c 'import json,sys; print(json.load(sys.stdin)["maintenanceToken"])')
fi

python "$(dirname "$0")/archive.py" backup --source "$DATA_ROOT" --output "$OUTPUT_DIR"
