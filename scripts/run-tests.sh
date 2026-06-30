#!/usr/bin/env bash
# Build app:ci → app:test from current source and run the test suite.
# Ensures images always reflect the working tree, not a stale cached build.
#
# Usage:
#   scripts/run-tests.sh           # build + run
#   scripts/run-tests.sh --no-run  # build only (to inspect the image)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

RUN=true
for arg in "$@"; do
  [[ "$arg" == "--no-run" ]] && RUN=false
done

# ── Validate working tree ────────────────────────────────────────────────────
SHORT_SHA=$(git rev-parse --short HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)

echo "=== Working tree ==="
echo "  Branch : $BRANCH"
echo "  SHA    : $SHORT_SHA"

DIRTY=$(git status --porcelain 2>/dev/null)
if [[ -n "$DIRTY" ]]; then
  echo ""
  echo "  WARNING: uncommitted changes are present."
  echo "  The Docker build will include them — this is intentional for local testing."
  echo "  Run 'git diff' or 'git status' to see what differs from HEAD."
  echo ""
fi

# ── Build production image ───────────────────────────────────────────────────
echo "=== Building app:ci ==="
docker build \
  --build-arg GIT_SHA="$SHORT_SHA" \
  -t app:ci \
  .

# ── Build test layer ─────────────────────────────────────────────────────────
echo "=== Building app:test ==="
docker build \
  --build-arg BASE_IMAGE=app:ci \
  -f Dockerfile.test \
  -t app:test \
  .

# ── Run tests ────────────────────────────────────────────────────────────────
if $RUN; then
  echo "=== Running tests ==="
  docker run --rm app:test
  echo ""
  echo "All tests passed."
fi
