#!/usr/bin/env bash
# Copies our customisation files from overrides/ to the upstream app directories.
# Run this after every git pull if librechat-admin-panel overrides changed.
#
# LibreChat itself no longer needs this: docker-compose.override.yml mounts
# librechat.yaml, branding, index.html, manifest.webmanifest, search.cjs/mjs,
# and the litellm/ config directly from overrides/librechat/ - a git pull on
# /srv is the whole deploy. AuthService.js/localStrategy.js/invite-fixed.js
# are tracked as real commits on apps/librechat's own submodule
# (dorianaitest/LibreChat, branch hetzner-ai-mvp-patches).
#
# Usage: cd /srv && ./overrides/deploy-overrides.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRV="$(dirname "$SCRIPT_DIR")"

echo "Deploying LibreChat admin panel overrides..."
cp "$SCRIPT_DIR/librechat-admin-panel/docker-compose.override.yml" "$SRV/apps/librechat-admin-panel/"

echo "Done. Restart affected containers if needed."
