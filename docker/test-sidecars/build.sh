#!/usr/bin/env bash
# Build pre-built test sidecar images.
# Run once on a new machine or after updating dependencies.
set -euo pipefail
cd "$(dirname "$0")"

echo "Building bizniz-test-pytest..."
docker build -t bizniz-test-pytest:latest -f Dockerfile.pytest .

echo "Building bizniz-test-playwright..."
docker build -t bizniz-test-playwright:latest -f Dockerfile.playwright .

echo "Done. Images:"
docker images --format "  {{.Repository}}:{{.Tag}} ({{.Size}})" | grep bizniz-test
