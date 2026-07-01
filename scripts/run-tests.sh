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
  # Persistent results directory — kept after the run for inspection.
  # Cleared at the start of each run so stale artifacts never accumulate.
  RESULTS_DIR="$SCRIPT_DIR/test-results"
  rm -rf "$RESULTS_DIR"
  mkdir -p "$RESULTS_DIR"
  chmod 777 "$RESULTS_DIR"   # allow non-root testrunner to write in pass 2

  # Pass 1 (root): full suite.  requires_root sandbox tests run; requires_non_root skip.
  # Coverage data written to .coverage.1 so pass-2 data can be combined separately.
  echo "=== Running tests (pass 1 of 2: root) ==="
  docker run --rm \
    -e COVERAGE_FILE=/test-results/.coverage.1 \
    -v "$RESULTS_DIR:/test-results" \
    app:test \
    python3.11 -m pytest tests/ \
      --junit-xml=/test-results/pass1.xml \
      --cov=app --cov-report= \
      --tb=short -q
  echo ""

  # Pass 2 (non-root): security module only.  requires_non_root tests now run.
  # Run the full module without a name filter so all requires_non_root tests fire.
  echo "=== Running tests (pass 2 of 2: non-root sandbox tests) ==="
  docker run --rm --user testrunner \
    -e COVERAGE_FILE=/test-results/.coverage.2 \
    -v "$RESULTS_DIR:/test-results" \
    app:test \
    python3.11 -m pytest /app/tests/test_security.py \
      --junit-xml=/test-results/pass2.xml \
      --cov=app --cov-report= \
      -p no:cacheprovider --tb=short -q --rootdir=/app
  echo ""

  # ── Combined coverage ──────────────────────────────────────────────────────
  # Merge the two coverage data files and emit a single combined report.
  # The source files live at /app inside the image, so we run from there.
  echo "=== Combined coverage ==="
  docker run --rm \
    -w /app \
    -v "$RESULTS_DIR:/test-results" \
    app:test \
    bash -c "
      coverage combine \
        --data-file /test-results/.coverage \
        /test-results/.coverage.1 \
        /test-results/.coverage.2 \
      && coverage report \
           --data-file /test-results/.coverage \
           -m \
      && coverage xml \
           --data-file /test-results/.coverage \
           -o /test-results/coverage.xml \
           -q
    "
  echo ""
  echo "  Coverage XML → $RESULTS_DIR/coverage.xml"
  echo ""

  # ── Per-test summary table ─────────────────────────────────────────────────
  python3 "$SCRIPT_DIR/summarize_test_results.py" \
    "$RESULTS_DIR/pass1.xml" \
    "$RESULTS_DIR/pass2.xml"

fi
