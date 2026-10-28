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
HEARTBEAT_PID=""

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

release_maintenance() {
  # release_maintenance SESSION: leave maintenance with the session's lease.
  session=$1
  LEASE_TOKEN=$(cat "$session/lease" 2>/dev/null || true)
  MAINTENANCE_TOKEN=$(cat "$session/maintenance")
  export LEASE_TOKEN MAINTENANCE_TOKEN
  json_of maintenanceToken=MAINTENANCE_TOKEN | curl --fail --silent --show-error --cookie "$session/cookie" -X POST \
    "$API_URL/api/v1/maintenance/release" \
    -H "X-Control-Lease-Token: $LEASE_TOKEN" -H "Content-Type: application/json" \
    --data-binary @- >/dev/null
}

resume_account() {
  session=$1
  released=1
  if [ -s "$session/maintenance" ]; then
    if ! release_maintenance "$session"; then
      # The lease may have expired during a long archive: take a fresh one
      # (nobody else can hold it while the account is paused) and retry.
      curl --fail --silent --show-error --cookie "$session/cookie" -X POST \
        "$API_URL/api/v1/control/lease" -H "Content-Type: application/json" \
        -d '{"clientId":"phase1-backup","actorId":"operator-backup"}' | \
        python -c 'import json,sys; print(json.load(sys.stdin)["leaseToken"])' > "$session/lease.new" \
        && mv "$session/lease.new" "$session/lease"
      release_maintenance "$session" || released=0
    fi
  fi
  LEASE_TOKEN=$(cat "$session/lease" 2>/dev/null || true)
  export LEASE_TOKEN
  if [ -n "$LEASE_TOKEN" ]; then
    json_of leaseToken=LEASE_TOKEN | curl --fail --silent --show-error --cookie "$session/cookie" -X DELETE \
      "$API_URL/api/v1/control/lease" -H "Content-Type: application/json" \
      --data-binary @- >/dev/null
  fi
  curl --silent --show-error --cookie "$session/cookie" -X POST \
    "$API_URL/api/v1/auth/logout" >/dev/null
  rm -f "$session/cookie"
  if [ "$released" -eq 0 ]; then
    echo "An account is still paused for maintenance. Its maintenance token is kept in $session/maintenance; release it with POST /api/v1/maintenance/release, or restart the service." >&2
    return 1
  fi
  rm -rf "$session"
}

# Renew every paused account's lease while the archive runs, so the release
# at the end is still allowed however long the archive takes.
keep_leases() {
  sleeper=""
  trap '[ -z "$sleeper" ] || kill "$sleeper" 2>/dev/null; exit 0' TERM
  while :; do
    sleep "${MANYSELVES_BACKUP_HEARTBEAT_SECONDS:-20}" &
    sleeper=$!
    wait "$sleeper"
    sleeper=""
    for session in "$SESSIONS"/*/; do
      [ -f "$session/lease" ] || continue
      LEASE_TOKEN=$(cat "$session/lease")
      export LEASE_TOKEN
      json_of leaseToken=LEASE_TOKEN | curl --fail --silent --show-error --cookie "$session/cookie" -X POST \
        "$API_URL/api/v1/control/lease/heartbeat" -H "Content-Type: application/json" \
        --data-binary @- >/dev/null || true
    done
  done
}

cleanup() {
  status=$?
  trap - EXIT INT TERM
  set +e
  if [ -n "$HEARTBEAT_PID" ]; then
    kill "$HEARTBEAT_PID" 2>/dev/null
    wait "$HEARTBEAT_PID" 2>/dev/null
  fi
  if [ -n "$SESSIONS" ]; then
    still_paused=0
    for session in "$SESSIONS"/*/; do
      [ -d "$session" ] || continue
      resume_account "${session%/}" || still_paused=1
    done
    if [ "$still_paused" -eq 0 ]; then
      rm -rf "$SESSIONS"
    else
      rm -f "$SESSIONS/accounts.tsv"
      [ "$status" -ne 0 ] || status=1
    fi
  fi
  exit "$status"
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

if [ -n "$SESSIONS" ]; then
  keep_leases >/dev/null 2>&1 &
  HEARTBEAT_PID=$!
fi

python "$ARCHIVE" backup --source "$DATA_ROOT" --output "$OUTPUT_DIR"
