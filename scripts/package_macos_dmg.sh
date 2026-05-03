#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIST_DIR="${DIST_DIR:-$ROOT_DIR/dist}"
BUILD_DIR="${BUILD_DIR:-$ROOT_DIR/build/package-macos}"
VERSION="${LABGPU_VERSION:-$(PYTHONPATH="$ROOT_DIR/src" python3 - <<'PY'
from labgpu import __version__
print(__version__)
PY
)}"
DMG_PATH="$DIST_DIR/LabGPU-${VERSION}-macOS.dmg"
STAGE="$BUILD_DIR/LabGPU-${VERSION}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$1" >&2
    exit 1
  }
}

need_cmd python3
need_cmd hdiutil

rm -rf "$STAGE"
mkdir -p "$STAGE" "$DIST_DIR"

PYTHONPATH="$ROOT_DIR/src" python3 -m labgpu.cli.main desktop --install-app "$STAGE/LabGPU.app"

cat > "$STAGE/install.command" <<'SH'
#!/bin/sh
set -eu
cd "$(dirname "$0")"
REPO_URL="${LABGPU_REPO_URL:-https://github.com/66LIU-frank/Labgpu-controller.git}"
printf 'Installing LabGPU from %s\n' "$REPO_URL"
if command -v pipx >/dev/null 2>&1; then
  pipx install --force "git+$REPO_URL"
else
  python3 -m pip install --user --upgrade "git+$REPO_URL"
fi
printf '\nInstalled. You can now open LabGPU.app or run: labgpu desktop\n'
read -r -p "Press Enter to close..." _
SH
chmod +x "$STAGE/install.command"

cat > "$STAGE/README.txt" <<TXT
LabGPU ${VERSION} for macOS

1. Run install.command once.
2. Open LabGPU.app.
3. LabGPU runs locally at 127.0.0.1 and opens an app-style browser window.

If the app says LabGPU is not installed, run install.command again or install:
  pipx install git+https://github.com/66LIU-frank/Labgpu-controller.git

LabGPU uses your normal ~/.ssh/config and does not install a daemon on remote servers.
TXT

rm -f "$DMG_PATH"
hdiutil create -volname "LabGPU" -srcfolder "$STAGE" -ov -format UDZO "$DMG_PATH"
printf 'Created %s\n' "$DMG_PATH"
