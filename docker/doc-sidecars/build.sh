#!/usr/bin/env bash
# Build pre-configured documenter sidecar images.
# Run once on a new machine or after updating the extract scripts.
set -euo pipefail
cd "$(dirname "$0")"

echo "Building bizniz-doc-typescript..."
docker build -t bizniz-doc-typescript:latest -f Dockerfile.typescript .

echo "Done. Images:"
docker images --format "  {{.Repository}}:{{.Tag}} ({{.Size}})" | grep bizniz-doc
