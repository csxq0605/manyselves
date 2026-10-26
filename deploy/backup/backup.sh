#!/bin/sh
set -eu

DATA_ROOT=${MANYSELVES_DATA_DIR:?set MANYSELVES_DATA_DIR}
OUTPUT_DIR=${1:-./backups}
API_URL=${MANYSELVES_API_URL:-}
ADMIN_USERNAME=${MANYSELVES_ADMIN_USERNAME:-admin}
ADMIN_PASSWORD=${MANYSELVES_ADMIN_PASSWORD:-}
# The archive covers the whole data root, so every account is paused first.
# With an accounts file each account logs in with the password in the
# environment variable the file names for it.
ACCOUNTS_FILE=${MANYSELVES_BACKUP_ACCOUNTS_FILE:-$DATA_ROOT/.manyselves/accounts.yaml}
ARCHIVE="$(dirname "$0")/archive.py"
SESSIONS=""
PAUSED=0

json_of() {
  # json_of KEY=ENVNAME ... prints {"KEY": $ENVNAME, ...}
  python -c 'import json, os, sys; print(json.dumps({k: os.environ[v] for k, v in (a.split("=", 1) for a in sys.argv[1:])}))' "$@"
}

# One directory per paused account: cookie, lease and maintenance token.
pause_account() {
  PAUSED=$((PAUSED + 1))
  session="$SESSIONS/$PAUSED"
  mkdir "$session"
  : > "$session/cookie"
  BACKUP_USER=$1 BACKUP_PASSWORD=$2
  export BACKUP_USER BACKUP_PASSWORD
  json_of username=BACKUP_USER password=BACKUP_PASSWORD | \
    curl --fail --silent --show-error --cookie-jar "$session/cookie" -X POST \
    "$API_URL/api/v1/auth/login" -H "Content-Type: application/json" \
    --data-binary @- >/dev/null
  curl --fail --silent --show-error --cookie "$session/cookie" -X POST \
    "$API_URL/api/v1/control/lease" -H "Content-Type: application/json" \
    -d '{"clientId":"phase1-backup","actorId":"operator-backup"}' | \
    python -c 'import json,sys; print(json.load(sys.stdin)["leaseToken"])' > "$session/lease"
  curl --fail --silent --show-error --cookie "$session/cookie" -X POST \
    "$API_URL/api/v1/maintenance/quiesce" -H "X-Control-Lease-Token: $(cat "$session/lease")" | \
    python -c 'import json,sys; print(json.load(sys.stdin)["maintenanceToken"])' > "$session/maintenance"
}

resume_account() {
  session=$1
  LEASE_TOKEN=$(cat "$session/lease" 2>/dev/null || true)
  MAINTENANCE_TOKEN=$(cat "$session/maintenance" 2>/dev/null || true)
  export LEASE_TOKEN MAINTENANCE_TOKEN
  if [ -n "$MAINTENANCE_TOKEN" ]; then
    json_of maintenanceToken=MAINTENANCE_TOKEN | curl --fail --silent --show-error --cookie "$session/cookie" -X POST \
      "$API_URL/api/v1/maintenance/release" \
      -H "X-Control-Lease-Token: $LEASE_TOKEN" -H "Content-Type: application/json" \
      --data-binary @- >/dev/null
  fi
  if [ -n "$LEASE_TOKEN" ]; then
    json_of leaseToken=LEASE_TOKEN | curl --fail --silent --show-error --cookie "$session/cookie" -X DELETE \
      "$API_URL/api/v1/control/lease" -H "Content-Type: application/json" \
      --data-binary @- >/dev/null
  fi
  curl --silent --show-error --cookie "$session/cookie" -X POST \
    "$API_URL/api/v1/auth/logout" >/dev/null
  rm -f "$session/cookie"
}

cleanup() {
  trap - EXIT INT TERM
  set +e
  if [ -n "$SESSIONS" ]; then
    for session in "$SESSIONS"/*; do
      [ -d "$session" ] && resume_account "$session"
    done
    rm -rf "$SESSIONS"
  fi
}
trap cleanup EXIT INT TERM

if [ -n "$API_URL" ]; then
  SESSIONS=$(mktemp -d)
  if [ -f "$ACCOUNTS_FILE" ]; then
    python "$ARCHIVE" accounts --file "$ACCOUNTS_FILE" > "$SESSIONS/accounts.tsv"
    while IFS="$(printf '\t')" read -r username password_env <&3; do
      password=$(printenv "$password_env" || true)
      [ -n "$password" ] || {
        echo "$password_env is required to pause account $username" >&2
        exit 2
      }
      pause_account "$username" "$password"
    done 3< "$SESSIONS/accounts.tsv"
  else
    [ -n "$ADMIN_PASSWORD" ] || {
      echo "MANYSELVES_ADMIN_PASSWORD is required with MANYSELVES_API_URL" >&2
      exit 2
    }
    pause_account "$ADMIN_USERNAME" "$ADMIN_PASSWORD"
  fi
fi

python "$ARCHIVE" backup --source "$DATA_ROOT" --output "$OUTPUT_DIR"
