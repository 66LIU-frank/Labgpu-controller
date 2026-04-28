#!/usr/bin/env sh
set -eu

REPO_URL="${LABGPU_REPO_URL:-https://github.com/66LIU-frank/Labgpu-controller.git}"
PACKAGE_SPEC="git+${REPO_URL}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || printf '.')

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'LabGPU install failed: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

find_labgpu() {
  if command -v labgpu >/dev/null 2>&1; then
    command -v labgpu
    return 0
  fi
  if [ -x "$HOME/.local/bin/labgpu" ]; then
    printf '%s\n' "$HOME/.local/bin/labgpu"
    return 0
  fi
  return 1
}

need_cmd python3

log "LabGPU installer"
log "Repository: $REPO_URL"

if [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/src/labgpu" ]; then
  PACKAGE_SPEC="$SCRIPT_DIR"
  log "Installing from local checkout: $SCRIPT_DIR"
else
  need_cmd git
  log "Installing from GitHub."
fi

if command -v pipx >/dev/null 2>&1; then
  log "Using pipx."
  pipx install --force "$PACKAGE_SPEC"
else
  log "pipx not found; using python3 -m pip --user."
  python3 -m pip install --user --upgrade "$PACKAGE_SPEC"
fi

LABGPU_BIN="$(find_labgpu || true)"
if [ -z "$LABGPU_BIN" ]; then
  log ""
  log "Installed, but 'labgpu' is not on PATH yet."
  log "Add this to your shell profile if needed:"
  log "  export PATH=\"\$HOME/.local/bin:\$PATH\""
  LABGPU_BIN="$HOME/.local/bin/labgpu"
fi

log ""
log "Installed LabGPU."
log "Binary: $LABGPU_BIN"
log ""
log "Try:"
log "  labgpu doctor"
log "  labgpu init"
log "  labgpu ui"
log ""
log "For a specific SSH host:"
log "  labgpu ui --hosts alpha_liu"
log ""
log "For multi-server setup:"
log "  labgpu init --hosts alpha_liu,song_1 --tags A100,training"
log "  labgpu ui"
