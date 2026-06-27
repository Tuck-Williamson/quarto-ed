#!/usr/bin/env bash
# Deploy a PR build from ghcr.io to the quarto-ed-test Heroku app.
#
# Usage: deploy-pr.sh <PR_NUMBER>
#
# Requires:
#   heroku CLI logged in  (`heroku login` or `heroku auth:token` working)
#   crane                 (install: go install github.com/google/go-containerregistry/cmd/crane@latest
#                          or: brew install crane)
#   GHCR_TOKEN (optional) — GitHub PAT with read:packages scope.
#                           Not needed if the ghcr.io package is public.
#
# Uses crane (not docker push) to copy the image because Docker's containerd
# storage backend wraps images in an OCI index that Heroku's registry rejects
# when pushed as a manifest list. crane copies the linux/amd64 manifest directly
# at the registry API level, bypassing that issue entirely.
set -euo pipefail

HEROKU_TEST_APP="quarto-ed-test"
GHCR_IMAGE="ghcr.io/tuck-williamson/quarto-ed"

PR_NUMBER="${1:-}"
if [[ -z "$PR_NUMBER" ]]; then
  echo "Usage: deploy-pr.sh <PR_NUMBER>"
  exit 1
fi

for dep in heroku crane; do
  if ! command -v "$dep" &>/dev/null; then
    echo "ERROR: $dep not found."
    [[ "$dep" == "heroku" ]] && echo "  Install: https://devcenter.heroku.com/articles/heroku-cli"
    [[ "$dep" == "crane" ]]  && echo "  Install: go install github.com/google/go-containerregistry/cmd/crane@latest"
    exit 1
  fi
done

PR_TAG="pr-${PR_NUMBER}"
GHCR_REF="${GHCR_IMAGE}:${PR_TAG}"
HEROKU_REF="registry.heroku.com/${HEROKU_TEST_APP}/web"

echo "=== Authenticating crane with Heroku registry ==="
heroku auth:token | crane auth login registry.heroku.com --username=_ --password-stdin

if [[ -n "${GHCR_TOKEN:-}" ]]; then
  echo "=== Authenticating crane with ghcr.io ==="
  echo "$GHCR_TOKEN" | crane auth login ghcr.io --username _ --password-stdin
fi

echo "=== Copying ${GHCR_REF} (linux/amd64) → ${HEROKU_REF} ==="
crane copy --platform linux/amd64 "$GHCR_REF" "$HEROKU_REF"

echo "=== Releasing to ${HEROKU_TEST_APP} ==="
heroku container:release web -a "$HEROKU_TEST_APP"

echo ""
echo "Deployed PR #${PR_NUMBER} to https://${HEROKU_TEST_APP}.herokuapp.com"
