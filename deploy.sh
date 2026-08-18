#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="${ROOT_DIR}/VERSION"
VERSION="${VERSION:-}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp}"

if [ -z "$VERSION" ]; then
  if [ -f "$VERSION_FILE" ]; then
    VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
  else
    VERSION="v0.0.1"
  fi
fi

cd "$ROOT_DIR"

echo "==> Building ManySelves offline release ${VERSION}"
./scripts/build-local.sh --version "$VERSION" --output-dir "$OUTPUT_DIR" "$@"

RELEASE_DIR="${OUTPUT_DIR}/manyselves-release-${VERSION}"

cat <<EOF

Release artifacts are ready:
  ${RELEASE_DIR}

Copy them to CentOS, for example:
  scp ${RELEASE_DIR}/manyselves-${VERSION}-* manyselves@SERVER:/tmp/

On CentOS, run as the dedicated service user:
  sudo -iu manyselves
  export XDG_RUNTIME_DIR=/run/user/\$(id -u)
  cd /tmp
  tar -xzf manyselves-${VERSION}-source.tar.gz
  cd manyselves-${VERSION}
  bash scripts/deploy-centos-podman.sh --version ${VERSION} --artifact-dir /tmp --public-host SERVER
EOF
