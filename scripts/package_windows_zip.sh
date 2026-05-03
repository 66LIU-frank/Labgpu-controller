#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIST_DIR="${DIST_DIR:-$ROOT_DIR/dist}"
BUILD_DIR="${BUILD_DIR:-$ROOT_DIR/build/package-windows}"
VERSION="${LABGPU_VERSION:-$(PYTHONPATH="$ROOT_DIR/src" python3 - <<'PY'
from labgpu import __version__
print(__version__)
PY
)}"
ZIP_PATH="$DIST_DIR/LabGPU-${VERSION}-Windows.zip"
STAGE="$BUILD_DIR/LabGPU-${VERSION}-Windows"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$1" >&2
    exit 1
  }
}

need_cmd python3

rm -rf "$STAGE"
mkdir -p "$STAGE" "$DIST_DIR"

cat > "$STAGE/Install-LabGPU.ps1" <<'PS1'
$ErrorActionPreference = "Stop"
$RepoUrl = if ($env:LABGPU_REPO_URL) { $env:LABGPU_REPO_URL } else { "https://github.com/66LIU-frank/Labgpu-controller.git" }
Write-Host "Installing LabGPU from $RepoUrl"

if (Get-Command pipx -ErrorAction SilentlyContinue) {
  pipx install --force "git+$RepoUrl"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
  py -3 -m pip install --user --upgrade "git+$RepoUrl"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  python -m pip install --user --upgrade "git+$RepoUrl"
} else {
  throw "Python 3 is required. Install Python first, then rerun this script."
}

Write-Host ""
Write-Host "Installed. Start LabGPU with Start-LabGPU.cmd or run: labgpu desktop"
Read-Host "Press Enter to close"
PS1

cat > "$STAGE/Start-LabGPU.cmd" <<'CMD'
@echo off
setlocal
where labgpu >nul 2>nul
if errorlevel 1 (
  echo LabGPU is not installed yet.
  echo Running installer...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-LabGPU.ps1"
)
where labgpu >nul 2>nul
if errorlevel 1 (
  echo.
  echo LabGPU still was not found on PATH.
  echo Try opening a new terminal, or run:
  echo   py -3 -m pip install --user --upgrade git+https://github.com/66LIU-frank/Labgpu-controller.git
  pause
  exit /b 1
)
labgpu desktop
CMD

cat > "$STAGE/Start-LabGPU.ps1" <<'PS1'
$ErrorActionPreference = "Stop"
if (-not (Get-Command labgpu -ErrorAction SilentlyContinue)) {
  & "$PSScriptRoot\Install-LabGPU.ps1"
}
labgpu desktop
PS1

cat > "$STAGE/README-Windows.txt" <<TXT
LabGPU ${VERSION} for Windows

1. Double-click Start-LabGPU.cmd.
2. If LabGPU is missing, the script runs Install-LabGPU.ps1.
3. LabGPU runs locally at 127.0.0.1 and opens your browser.

Requirements:
- Windows 10/11
- Python 3
- OpenSSH client
- Your normal SSH config/keys

LabGPU uses your normal SSH setup and does not install a daemon on remote servers.
TXT

rm -f "$ZIP_PATH"
STAGE="$STAGE" ZIP_PATH="$ZIP_PATH" python3 - <<'PY'
import os
import zipfile
from pathlib import Path

stage = Path(os.environ["STAGE"])
zip_path = Path(os.environ["ZIP_PATH"])
with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(stage.parent))
print(f"Created {zip_path}")
PY
