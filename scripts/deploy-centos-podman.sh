#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VERSION_FILE="${SOURCE_ROOT}/VERSION"

DEFAULT_VERSION="v0.0.1"
VERSION=""
ARTIFACT_DIR="/tmp"
INSTALL_DIR="/opt/manyselves"
DATA_DIR="/srv/manyselves/data"
PUBLIC_HOST="192.168.8.28"
HTTP_PORT="9090"
RESTORE_DATA=0

usage() {
  cat <<'EOF'
Usage: scripts/deploy-centos-podman.sh [options]

Deploy a prebuilt ManySelves release on CentOS/RHEL with rootless Podman.

Run this as the dedicated service user, for example:
  sudo -iu manyselves
  export XDG_RUNTIME_DIR=/run/user/$(id -u)
  cd /tmp/manyselves-v0.0.1
  ./scripts/deploy-centos-podman.sh --version v0.0.1 --artifact-dir /tmp

Options:
  --version VERSION     Release version, for example v0.0.1.
  --artifact-dir DIR    Directory containing release archives. Default: /tmp.
  --install-dir DIR     Parent install directory. Default: /opt/manyselves.
  --data-dir DIR        Persistent data directory. Default: /srv/manyselves/data.
  --public-host HOST    Browser-facing host/IP for allowed origins. Default: 192.168.8.28.
  --port PORT           Browser-facing HTTP port. Default: 9090.
  --restore-data        Restore manyselves-<version>-data.tar.gz if present.
  -h, --help            Show this help.

Required artifacts:
  manyselves-<version>-linux-amd64-images.tar
  manyselves-<version>-source.tar.gz
  manyselves-<version>.sha256
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

info() {
  echo "==> $*"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version)
      VERSION="${2:?--version requires a value}"
      shift 2
      ;;
    --artifact-dir)
      ARTIFACT_DIR="${2:?--artifact-dir requires a value}"
      shift 2
      ;;
    --install-dir)
      INSTALL_DIR="${2:?--install-dir requires a value}"
      shift 2
      ;;
    --data-dir)
      DATA_DIR="${2:?--data-dir requires a value}"
      shift 2
      ;;
    --public-host)
      PUBLIC_HOST="${2:?--public-host requires a value}"
      shift 2
      ;;
    --port)
      HTTP_PORT="${2:?--port requires a value}"
      shift 2
      ;;
    --restore-data)
      RESTORE_DATA=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$VERSION" ]; then
  if [ -f "$VERSION_FILE" ]; then
    VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
  else
    VERSION="$DEFAULT_VERSION"
  fi
fi

if ! [[ "$VERSION" =~ ^v[0-9]+[.][0-9]+[.][0-9]+([-+][A-Za-z0-9._-]+)?$ ]]; then
  fail "invalid version '${VERSION}'. Expected format like v0.0.1."
fi

ARTIFACT_DIR="$(cd "$ARTIFACT_DIR" && pwd)"
IMAGES_ARCHIVE="${ARTIFACT_DIR}/manyselves-${VERSION}-linux-amd64-images.tar"
SOURCE_ARCHIVE="${ARTIFACT_DIR}/manyselves-${VERSION}-source.tar.gz"
DATA_ARCHIVE="${ARTIFACT_DIR}/manyselves-${VERSION}-data.tar.gz"
CHECKSUM_FILE="${ARTIFACT_DIR}/manyselves-${VERSION}.sha256"
APP_DIR="${INSTALL_DIR%/}/manyselves-${VERSION}"
ENV_FILE="${APP_DIR}/deploy/.env"

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

compose() {
  if podman compose version >/dev/null 2>&1; then
    podman compose "$@"
  elif command -v podman-compose >/dev/null 2>&1; then
    podman-compose "$@"
  else
    fail "missing Podman Compose provider. Install podman-compose or a Podman version with 'podman compose'."
  fi
}

ensure_service_user_context() {
  if [ "$(id -u)" -eq 0 ]; then
    fail "do not run this script as root. Run it as the dedicated service user, such as 'manyselves'."
  fi

  if ! grep -q "^$(id -un):" /etc/subuid 2>/dev/null || ! grep -q "^$(id -un):" /etc/subgid 2>/dev/null; then
    cat >&2 <<EOF
Rootless Podman subuid/subgid ranges are missing for user $(id -un).
Ask an administrator to run:
  sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $(id -un)
  sudo loginctl enable-linger $(id -un)
Then log in again and run:
  podman system migrate
EOF
    exit 1
  fi

  if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  fi
  if [ ! -d "$XDG_RUNTIME_DIR" ]; then
    cat >&2 <<EOF
XDG_RUNTIME_DIR is not available: ${XDG_RUNTIME_DIR}
Ask an administrator to run:
  sudo loginctl enable-linger $(id -un)
Then start a fresh service-user session:
  sudo -iu $(id -un)
  export XDG_RUNTIME_DIR=/run/user/$(id -u)
EOF
    exit 1
  fi
}

ensure_directory() {
  local path="$1"
  local mode_hint="$2"

  if mkdir -p "$path" 2>/dev/null; then
    return
  fi

  cat >&2 <<EOF
Cannot create or write ${path}.
Ask an administrator to prepare it first:
  sudo install -d -m ${mode_hint} -o $(id -un) -g $(id -gn) ${path}
EOF
  exit 1
}

upsert_env() {
  local key="$1"
  local value="$2"
  local file="$3"
  local temp_file

  temp_file="$(mktemp)"
  if grep -q "^${key}=" "$file"; then
    awk -v key="$key" -v value="$value" '
      $0 ~ "^" key "=" {
        print key "=" value
        next
      }
      { print }
    ' "$file" > "$temp_file"
  else
    cat "$file" > "$temp_file"
    printf '%s=%s\n' "$key" "$value" >> "$temp_file"
  fi
  cat "$temp_file" > "$file"
  rm -f "$temp_file"
}

detect_image() {
  local repository="$1"
  local tag="$2"

  if podman image exists "localhost/${repository}:${tag}"; then
    printf 'localhost/%s:%s\n' "$repository" "$tag"
  elif podman image exists "${repository}:${tag}"; then
    printf '%s:%s\n' "$repository" "$tag"
  else
    fail "loaded image not found: ${repository}:${tag}"
  fi
}

container_uid_gid() {
  local image="$1"
  podman run --rm --entrypoint sh "$image" -c 'printf "%s:%s\n" "$(id -u)" "$(id -g)"'
}

label_data_dir_for_selinux() {
  if command -v chcon >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo chcon -Rt container_file_t "$DATA_DIR" 2>/dev/null || true
  else
    info "Skipping SELinux relabel helper; compose mount still uses :Z."
  fi
}

for command_name in awk chmod cp date grep id mkdir mktemp mv podman rmdir seq sha256sum sleep tar tr; do
  require_command "$command_name"
done

ensure_service_user_context

[ -f "$IMAGES_ARCHIVE" ] || fail "missing image archive: ${IMAGES_ARCHIVE}"
[ -f "$SOURCE_ARCHIVE" ] || fail "missing source archive: ${SOURCE_ARCHIVE}"
[ -f "$CHECKSUM_FILE" ] || fail "missing checksum file: ${CHECKSUM_FILE}"

info "Verifying release checksums"
(
  cd "$ARTIFACT_DIR"
  sha256sum -c "$(basename "$CHECKSUM_FILE")"
)

ensure_directory "$INSTALL_DIR" "0750"
ensure_directory "$DATA_DIR" "0700"

info "Loading container images"
podman load --input "$IMAGES_ARCHIVE"

API_IMAGE="$(detect_image manyselves-api "$VERSION")"
WEB_IMAGE="$(detect_image manyselves-web "$VERSION")"

info "Installing source release to ${APP_DIR}"
TEMP_EXTRACT_DIR="$(mktemp -d)"
tar -xzf "$SOURCE_ARCHIVE" -C "$TEMP_EXTRACT_DIR"

if [ ! -d "${TEMP_EXTRACT_DIR}/manyselves-${VERSION}" ]; then
  fail "source archive must contain top-level directory manyselves-${VERSION}"
fi

ENV_BACKUP=""
if [ -f "$ENV_FILE" ]; then
  ENV_BACKUP="$(mktemp)"
  cp "$ENV_FILE" "$ENV_BACKUP"
fi

if [ -d "$APP_DIR" ]; then
  mv "$APP_DIR" "${APP_DIR}.previous.$(date +%Y%m%d%H%M%S)"
fi
mv "${TEMP_EXTRACT_DIR}/manyselves-${VERSION}" "$APP_DIR"
rmdir "$TEMP_EXTRACT_DIR"

if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/deploy/env.example" "$ENV_FILE"
fi
if [ -n "$ENV_BACKUP" ]; then
  cp "$ENV_BACKUP" "$ENV_FILE"
  rm -f "$ENV_BACKUP"
fi
chmod 0600 "$ENV_FILE"

cd "$APP_DIR"

upsert_env "MANYSELVES_DATA_DIR" "$DATA_DIR" "$ENV_FILE"
upsert_env "MANYSELVES_ALLOWED_ORIGINS" "[\"http://${PUBLIC_HOST}:${HTTP_PORT}\"]" "$ENV_FILE"
upsert_env "MANYSELVES_HTTP_BIND" "0.0.0.0" "$ENV_FILE"
upsert_env "MANYSELVES_HTTP_PORT" "$HTTP_PORT" "$ENV_FILE"
upsert_env "MANYSELVES_API_IMAGE" "$API_IMAGE" "$ENV_FILE"
upsert_env "MANYSELVES_WEB_IMAGE" "$WEB_IMAGE" "$ENV_FILE"

if [ "$RESTORE_DATA" -eq 1 ]; then
  [ -f "$DATA_ARCHIVE" ] || fail "--restore-data was set, but data archive is missing: ${DATA_ARCHIVE}"
  info "Restoring data archive to ${DATA_DIR}"
  tar -xzf "$DATA_ARCHIVE" -C "$DATA_DIR"
fi

info "Fixing rootless Podman volume ownership"
CONTAINER_OWNER="$(container_uid_gid "$API_IMAGE")"
podman unshare chown -R "$CONTAINER_OWNER" "$DATA_DIR"
podman unshare chmod -R u+rwX,g+rwX,o-rwx "$DATA_DIR"
label_data_dir_for_selinux

info "Starting ManySelves with Podman Compose"
compose -f deploy/compose.yaml --env-file "$ENV_FILE" down 2>/dev/null || true
compose -f deploy/compose.yaml --env-file "$ENV_FILE" up -d --no-build --force-recreate

info "Current containers"
podman ps --filter name=manyselves

if command -v curl >/dev/null 2>&1; then
  info "Waiting for health endpoint"
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${HTTP_PORT}/api/v1/health/ready" >/dev/null 2>&1; then
      echo "ManySelves is ready: http://${PUBLIC_HOST}:${HTTP_PORT}"
      exit 0
    fi
    sleep 2
  done
  echo "Health check did not become ready yet. Inspect logs with:" >&2
  echo "  podman compose -f ${APP_DIR}/deploy/compose.yaml --env-file ${ENV_FILE} logs -f --tail 200 api web" >&2
  exit 1
else
  echo "curl is not installed; skipped HTTP health check."
  echo "ManySelves should be available at: http://${PUBLIC_HOST}:${HTTP_PORT}"
fi
