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
  RESULTS_DIR=$(mktemp -d)
  chmod 777 "$RESULTS_DIR"        # allow non-root testrunner to write in pass 2
  trap 'rm -rf "$RESULTS_DIR"' EXIT

  # Pass 1 (root): full suite.  requires_root sandbox tests run; requires_non_root skip.
  echo "=== Running tests (pass 1 of 2: root) ==="
  docker run --rm \
    -v "$RESULTS_DIR:/test-results" \
    app:test \
    python3.11 -m pytest tests/ \
      --junit-xml=/test-results/pass1.xml \
      --tb=short -q
  echo ""

  # Pass 2 (non-root): security module only.  requires_non_root tests now run.
  # Run the full module without a name filter so all 5 requires_non_root tests fire.
  echo "=== Running tests (pass 2 of 2: non-root sandbox tests) ==="
  docker run --rm --user testrunner \
    -v "$RESULTS_DIR:/test-results" \
    app:test \
    python3.11 -m pytest /app/tests/test_security.py \
      --junit-xml=/test-results/pass2.xml \
      -p no:cacheprovider --tb=short -q --no-cov --rootdir=/app
  echo ""

  # ── Per-test summary table ────────────────────────────────────────────────
  python3 - "$RESULTS_DIR/pass1.xml" "$RESULTS_DIR/pass2.xml" <<'PYEOF'
import sys, os, xml.etree.ElementTree as ET

RED_BOLD = "\033[1;31m"
YELLOW   = "\033[1;33m"
GREEN    = "\033[0;32m"
BLINK    = "\033[5;30;41m"   # blinking black on red — sanity-check fallback
RESET    = "\033[0m"


def load(path):
    """Return {nodeid: outcome} from a JUnit XML file."""
    results = {}
    if not os.path.exists(path):
        return results
    for tc in ET.parse(path).iter("testcase"):
        cls  = tc.attrib.get("classname", "")
        name = tc.attrib.get("name", "?")
        key  = f"{cls}::{name}" if cls else name
        if tc.find("failure") is not None or tc.find("error") is not None:
            outcome = "failed"
        elif tc.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        results[key] = outcome
    return results


p1 = load(sys.argv[1])
p2 = load(sys.argv[2])

all_ids = sorted(set(list(p1) + list(p2)))

# Column widths
NAME_W = max((len(k.split("::")[-1]) for k in all_ids), default=10)
NAME_W = max(NAME_W, len("Test Name"))
COL_W  = 6


def fmt(stages):
    return ",".join(str(s) for s in stages) if stages else "-"


def color(passes, fails):
    if fails:
        return RED_BOLD
    if not passes:   # skips only
        return YELLOW
    if passes:       # passed (± skips), no failures
        return GREEN
    return BLINK     # unexpected state


sep = f"+-{'-'*NAME_W}-+-{'-'*COL_W}-+-{'-'*COL_W}-+-{'-'*COL_W}-+"
hdr = f"| {'Test Name':<{NAME_W}} | {'Pass':^{COL_W}} | {'Fail':^{COL_W}} | {'Skip':^{COL_W}} |"
print(sep)
print(hdr)
print(sep)

totals = {"pass": 0, "fail": 0, "skip_only": 0}
for nid in all_ids:
    name   = nid.split("::")[-1]
    passes = [s for s, d in ((1, p1), (2, p2)) if d.get(nid) == "passed"]
    fails  = [s for s, d in ((1, p1), (2, p2)) if d.get(nid) == "failed"]
    skips  = [s for s, d in ((1, p1), (2, p2)) if d.get(nid) == "skipped"]

    clr  = color(passes, fails)
    line = f"| {name:<{NAME_W}} | {fmt(passes):^{COL_W}} | {fmt(fails):^{COL_W}} | {fmt(skips):^{COL_W}} |"
    print(f"{clr}{line}{RESET}")

    if fails:
        totals["fail"] += 1
    elif not passes:
        totals["skip_only"] += 1
    else:
        totals["pass"] += 1

print(sep)
print(
    f"\nTotal: {len(all_ids)}  "
    f"{GREEN}passed: {totals['pass']}{RESET}  "
    f"{YELLOW}skip-only: {totals['skip_only']}{RESET}  "
    f"{RED_BOLD}failures: {totals['fail']}{RESET}"
)

if totals["fail"]:
    sys.exit(1)
PYEOF

fi
