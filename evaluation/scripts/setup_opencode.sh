#!/usr/bin/env bash
# Install the pinned OpenCode binary, its plugin runtime, and Playwright MCP under runtime/opencode.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$ROOT/runtime/opencode"
VERSION="${OPENCODE_VERSION:-1.17.7}"

mkdir -p "$RUNTIME/bin" "$RUNTIME/install-home" "$RUNTIME/config/opencode"
installer="$(mktemp)"
trap 'rm -f "$installer"' EXIT
curl -fsSL --max-time 60 https://opencode.ai/install -o "$installer"
HOME="$RUNTIME/install-home" bash "$installer" --version "$VERSION" --no-modify-path
install -m 0755 "$RUNTIME/install-home/.opencode/bin/opencode" "$RUNTIME/bin/opencode"

cp "$ROOT/scripts/opencode-runtime/plugin/package.json" "$ROOT/scripts/opencode-runtime/plugin/package-lock.json" "$RUNTIME/config/opencode/"
cp "$ROOT/scripts/opencode-runtime/playwright/package.json" "$ROOT/scripts/opencode-runtime/playwright/package-lock.json" "$RUNTIME/"
npm ci --prefix "$RUNTIME/config/opencode" --no-audit --no-fund
npm ci --prefix "$RUNTIME" --no-audit --no-fund

test "$("$RUNTIME/bin/opencode" --version)" = "$VERSION"
echo "OpenCode $VERSION ready in $RUNTIME"
