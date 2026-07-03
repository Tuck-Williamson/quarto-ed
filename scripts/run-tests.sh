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

  # ── Passes 1–3 run concurrently (separate containers, shared results vol) ────
  # Each container writes its own coverage data file and JUnit XML, and its
  # console output to a per-pass log so the parallel streams stay readable.
  # Stage-three analysis (coverage combine + summary) runs only after all
  # complete.
  #
  #   Pass 1 (root):      full fast suite; requires_root run, requires_non_root skip.
  #   Pass 2 (non-root):  test_security.py as testrunner; requires_non_root run.
  #   Pass 3 (PDF, root): slow `-m pdf` TinyTeX renders (RUN_PDF_TESTS=1). Runs
  #                       with --no-cov (it shells out to quarto, no app code).
  echo "=== Running tests (passes 1–3 in parallel) ==="

  docker run --rm \
    -e COVERAGE_FILE=/test-results/.coverage.1 \
    -v "$RESULTS_DIR:/test-results" \
    app:test \
    python3.11 -m pytest tests/ \
      --junit-xml=/test-results/pass1.xml \
      --cov=app --cov-report= \
      --tb=short -q \
    > "$RESULTS_DIR/pass1.log" 2>&1 &
  PID1=$!

  docker run --rm --user testrunner \
    -e COVERAGE_FILE=/test-results/.coverage.2 \
    -v "$RESULTS_DIR:/test-results" \
    app:test \
    python3.11 -m pytest /app/tests/test_security.py \
      --junit-xml=/test-results/pass2.xml \
      --cov=app --cov-report= \
      -p no:cacheprovider --tb=short -q --rootdir=/app \
    > "$RESULTS_DIR/pass2.log" 2>&1 &
  PID2=$!

  docker run --rm \
    -e RUN_PDF_TESTS=1 \
    -v "$RESULTS_DIR:/test-results" \
    app:test \
    python3.11 -m pytest tests/ -m pdf \
      --junit-xml=/test-results/pass3.xml \
      --no-cov \
      --tb=short -q \
    > "$RESULTS_DIR/pass3.log" 2>&1 &
  PID3=$!

  wait "$PID1"; RC1=$?
  wait "$PID2"; RC2=$?
  wait "$PID3"; RC3=$?

  for n in 1 2 3; do
    echo "----- pass $n output -----"
    cat "$RESULTS_DIR/pass${n}.log"
    echo ""
  done

  # ── Stage 3: combined coverage (passes 1 & 2; pass 3 has --no-cov) ───────────
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
    "$RESULTS_DIR/pass2.xml" \
    "$RESULTS_DIR/pass3.xml"

  if [ "$RC1" -ne 0 ] || [ "$RC2" -ne 0 ] || [ "$RC3" -ne 0 ]; then
    echo ""
    echo "=== TESTS FAILED (pass1=$RC1 pass2=$RC2 pass3=$RC3) ==="
    exit 1
  fi
  echo "=== All passes green ==="

fi
