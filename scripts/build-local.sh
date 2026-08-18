#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="${ROOT_DIR}/VERSION"
DEFAULT_VERSION="v0.0.1"
OUTPUT_DIR="${TMPDIR:-/tmp}"
WITH_DATA=0
ALLOW_DIRTY=0
VERSION=""

usage() {
  cat <<'EOF'
Usage: scripts/build-local.sh [options]

Build offline CentOS deployment artifacts from the local machine.

Options:
  --version VERSION   Release version tag, for example v0.0.2.
  --output-dir DIR    Parent output directory. Default: /tmp.
  --with-data         Also package deploy/data as a separate data archive.
  --allow-dirty       Allow packaging when git has uncommitted changes.
  -h, --help          Show this help.

Outputs:
  <output-dir>/manyselves-release-<version>/
    manyselves-<version>-linux-amd64-images.tar
    manyselves-<version>-source.tar.gz
    manyselves-<version>-data.tar.gz       only with --with-data
    manyselves-<version>.sha256
    manyselves-build.env
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version)
      VERSION="${2:?--version requires a value}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="${2:?--output-dir requires a value}"
      shift 2
      ;;
    --with-data)
      WITH_DATA=1
      shift
      ;;
    --allow-dirty)
      ALLOW_DIRTY=1
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

cd "$ROOT_DIR"

if [ -z "$VERSION" ]; then
  if [ -f "$VERSION_FILE" ]; then
    VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
  else
    VERSION="$DEFAULT_VERSION"
  fi
fi

if ! [[ "$VERSION" =~ ^v[0-9]+[.][0-9]+[.][0-9]+([-+][A-Za-z0-9._-]+)?$ ]]; then
  echo "Invalid version '$VERSION'. Expected format like v0.0.1." >&2
  exit 2
fi

for command_name in git docker sha256sum tar; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    exit 127
  fi
done

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required. Enable Docker Desktop WSL integration or install Docker Compose." >&2
  exit 127
fi

if [ "$ALLOW_DIRTY" -ne 1 ] && [ -n "$(git status --porcelain)" ]; then
  echo "Git working tree is not clean. Commit changes first, or pass --allow-dirty intentionally." >&2
  git status --short >&2
  exit 3
fi

RELEASE_DIR="${OUTPUT_DIR}/manyselves-release-${VERSION}"
IMAGES_ARCHIVE="${RELEASE_DIR}/manyselves-${VERSION}-linux-amd64-images.tar"
SOURCE_ARCHIVE="${RELEASE_DIR}/manyselves-${VERSION}-source.tar.gz"
DATA_ARCHIVE="${RELEASE_DIR}/manyselves-${VERSION}-data.tar.gz"
CHECKSUM_FILE="${RELEASE_DIR}/manyselves-${VERSION}.sha256"
BUILD_ENV="${RELEASE_DIR}/manyselves-build.env"

API_IMAGE="manyselves-api:${VERSION}"
WEB_IMAGE="manyselves-web:${VERSION}"

rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"

cat > "$BUILD_ENV" <<EOF
MANYSELVES_DATA_DIR=/tmp/manyselves-build-data
MANYSELVES_ALLOWED_ORIGINS=["http://127.0.0.1:9090"]
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=build-only
MANYSELVES_OPENAI_API_KEY=
MANYSELVES_API_IMAGE=${API_IMAGE}
MANYSELVES_WEB_IMAGE=${WEB_IMAGE}
EOF

echo "==> Building linux/amd64 images for ${VERSION}"
DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose \
  -f deploy/compose.yaml \
  --env-file "$BUILD_ENV" \
  build

echo "==> Saving images"
docker save -o "$IMAGES_ARCHIVE" "$API_IMAGE" "$WEB_IMAGE"

echo "==> Creating source archive from git HEAD"
git archive \
  --format=tar.gz \
  --output="$SOURCE_ARCHIVE" \
  --prefix="manyselves-${VERSION}/" \
  HEAD

if [ "$WITH_DATA" -eq 1 ]; then
  if [ ! -d deploy/data ]; then
    echo "deploy/data does not exist; cannot package data." >&2
    exit 4
  fi
  echo "==> Creating data archive from deploy/data"
  tar -C deploy/data -czf "$DATA_ARCHIVE" .
fi

echo "==> Writing checksums"
(
  cd "$RELEASE_DIR"
  if [ "$WITH_DATA" -eq 1 ]; then
    sha256sum \
      "manyselves-${VERSION}-linux-amd64-images.tar" \
      "manyselves-${VERSION}-source.tar.gz" \
      "manyselves-${VERSION}-data.tar.gz" \
      > "manyselves-${VERSION}.sha256"
  else
    sha256sum \
      "manyselves-${VERSION}-linux-amd64-images.tar" \
      "manyselves-${VERSION}-source.tar.gz" \
      > "manyselves-${VERSION}.sha256"
  fi
)

echo
echo "Release artifacts:"
ls -lh "$RELEASE_DIR"
echo
echo "Copy to CentOS, for example:"
echo "  scp ${RELEASE_DIR}/manyselves-${VERSION}-* your_user@SERVER:/tmp/"
echo
echo "Use these image names in deploy/.env after podman load:"
echo "  MANYSELVES_API_IMAGE=localhost/manyselves-api:${VERSION}"
echo "  MANYSELVES_WEB_IMAGE=localhost/manyselves-web:${VERSION}"
