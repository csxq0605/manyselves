#!/bin/sh
set -eu

[ "${MANYSELVES_SERVICES_STOPPED:-}" = "yes" ] || {
  echo "Refusing restore: stop Compose and set MANYSELVES_SERVICES_STOPPED=yes" >&2
  exit 2
}
DATA_ROOT=${MANYSELVES_DATA_DIR:?set MANYSELVES_DATA_DIR}
ARCHIVE=${1:?usage: restore.sh ARCHIVE [--force]}
FORCE=${2:-}
if [ "$FORCE" = "--force" ]; then
  python "$(dirname "$0")/archive.py" restore --archive "$ARCHIVE" --target "$DATA_ROOT" --force
else
  python "$(dirname "$0")/archive.py" restore --archive "$ARCHIVE" --target "$DATA_ROOT"
fi
