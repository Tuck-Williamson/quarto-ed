#!/usr/bin/env bash
# Run a Trivy image scan matching CI settings: CRITICAL severity, ignore-unfixed,
# honour .trivyignore. Exits non-zero if any unignored CRITICAL CVEs are found.
#
# Usage: trivy-scan.sh [IMAGE]
#   IMAGE defaults to app:ci
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE="${1:-app:ci}"

if ! command -v trivy &>/dev/null; then
  echo "trivy not found locally — running via Docker"
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$REPO_ROOT/.trivyignore:/root/.trivyignore:ro" \
    aquasec/trivy:latest image \
      --format table \
      --exit-code 1 \
      --severity CRITICAL \
      --ignore-unfixed \
      --ignorefile /root/.trivyignore \
      "$IMAGE"
else
  trivy image \
    --format table \
    --exit-code 1 \
    --severity CRITICAL \
    --ignore-unfixed \
    --ignorefile "$REPO_ROOT/.trivyignore" \
    "$IMAGE"
fi
