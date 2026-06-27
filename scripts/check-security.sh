#!/usr/bin/env bash
# Run the static security checks that CI runs before the Docker build:
# pip-audit (dependency CVEs) and bandit (Python static analysis).
# Fast — no Docker needed.
#
# Uses the tool directly if already on PATH; falls back to `pipx run`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

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

echo ""
echo "Static security checks passed."
