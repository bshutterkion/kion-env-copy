#!/usr/bin/env bash
set -euo pipefail
PROVIDER_DIR="${1:-/Users/bshutter@kion.io/Dev/code/kion/kion/delivery-support/dev-tools/terraform-provider/new-terraform-provider}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/kion/meta/vendor"
mkdir -p "$DEST"
for f in generator_config.yaml crud_archetypes.yaml memberships.yaml; do
  cp "$PROVIDER_DIR/codegen/$f" "$DEST/$f"
done
git -C "$PROVIDER_DIR" rev-parse --short HEAD > "$DEST/VERSION" 2>/dev/null || echo "unknown" > "$DEST/VERSION"
echo "Vendored provider metadata @ $(cat "$DEST/VERSION")"
