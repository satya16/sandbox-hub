#!/usr/bin/env bash
# Build every image sandbox-hub needs, then start the hub.
# Usage: ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Building resource images (rest-api, mcp-server, oauth-provider)..."
docker compose --profile build-only build

echo "==> Building and starting the hub (this also builds the UI)..."
docker compose up -d --build hub

echo
echo "sandbox-hub is running: http://localhost:8090"
