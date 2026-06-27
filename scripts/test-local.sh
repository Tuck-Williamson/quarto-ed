#!/usr/bin/env bash
# Run the full CI pipeline locally: security scans → build → trivy → tests.
# Mirrors .github/workflows/docker-publish.yml so failures surface before push.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

SHORT_SHA=$(git rev-parse --short HEAD)

_run() {
  local tool="$1"; shift
  if command -v "$tool" &>/dev/null; then
    "$tool" "$@"
  elif command -v pipx &>/dev/null; then
    pipx run "$tool" "$@"
  else
    echo "ERROR: $tool not found and pipx is not available. Install pipx or $tool."
    exit 1
  fi
}

echo "=== pip-audit ==="
_run pip-audit -r requirements.txt

echo "=== bandit ==="
_run bandit -r app/ -c .bandit -ll

echo "=== docker build (app:ci) ==="
docker build \
  --build-arg GIT_SHA="$SHORT_SHA" \
  -t app:ci \
  .

echo "=== trivy (CRITICAL, ignore-unfixed) ==="
"$SCRIPT_DIR/trivy-scan.sh" app:ci

echo "=== docker build (app:test) ==="
docker build \
  --build-arg BASE_IMAGE=app:ci \
  -f Dockerfile.test \
  -t app:test \
  .

echo "=== pytest ==="
docker run --rm app:test

echo ""
echo "All checks passed."
