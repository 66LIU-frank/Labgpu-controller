#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "labgpu" / "cli" / "standalone.py"
DIST = ROOT / "dist" / "release"
BUILD = ROOT / "build" / "standalone"
APP_NAME = "LabGPU"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build standalone LabGPU desktop artifacts with PyInstaller.")
    parser.add_argument("--version", default=read_version(), help="version string for artifact names")
    parser.add_argument("--no-install", action="store_true", help="require an existing PyInstaller instead of installing into a build venv")
    parser.add_argument("--skip-tests", action="store_true", help="skip unit tests before packaging")
    parser.add_argument("--clean", action="store_true", help="remove previous standalone build output first")
    args = parser.parse_args(argv)

    if args.clean:
        shutil.rmtree(BUILD, ignore_errors=True)
    DIST.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)

    if not args.skip_tests:
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], env=source_env())
        run([sys.executable, "-m", "compileall", "-q", "src", "tests"])

    pyinstaller = find_or_prepare_pyinstaller(no_install=args.no_install)
    system = platform.system()
    if system == "Darwin":
        build_macos(pyinstaller, args.version)
    elif system == "Windows":
        build_windows(pyinstaller, args.version)
    else:
        build_linux(pyinstaller, args.version)
    return 0


def read_version() -> str:
    namespace: dict[str, str] = {}
    exec((ROOT / "src" / "labgpu" / "__init__.py").read_text(encoding="utf-8"), namespace)
    return str(namespace.get("__version__", "0.0.0"))


def source_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT / "src") if not existing else f"{ROOT / 'src'}{os.pathsep}{existing}"
    return env


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def find_or_prepare_pyinstaller(*, no_install: bool) -> list[str]:
    try:
        import PyInstaller  # noqa: F401
    except ModuleNotFoundError:
        if no_install:
            raise SystemExit("PyInstaller is not installed. Remove --no-install or install pyinstaller first.")
    else:
        return [sys.executable, "-m", "PyInstaller"]

    venv_dir = BUILD / "pyinstaller-venv"
    python_bin = venv_dir / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")
    if not python_bin.exists():
        run([sys.executable, "-m", "venv", str(venv_dir)])
    run([str(python_bin), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(python_bin), "-m", "pip", "install", "--upgrade", "pyinstaller"])
    run([str(python_bin), "-m", "pip", "install", "-e", str(ROOT)])
    return [str(python_bin), "-m", "PyInstaller"]


def pyinstaller_base(pyinstaller: list[str]) -> list[str]:
    return [
        *pyinstaller,
        "--noconfirm",
        "--clean",
        "--name",
        APP_NAME,
        "--paths",
        str(ROOT / "src"),
        "--collect-submodules",
        "labgpu",
        "--distpath",
        str(BUILD / "pyinstaller-dist"),
        "--workpath",
        str(BUILD / "pyinstaller-work"),
    ]


def build_macos(pyinstaller: list[str], version: str) -> None:
    run([*pyinstaller_base(pyinstaller), "--windowed", str(SRC)])
    app_path = BUILD / "pyinstaller-dist" / f"{APP_NAME}.app"
    if not app_path.exists():
        raise SystemExit(f"expected app bundle missing: {app_path}")
    stage = BUILD / f"{APP_NAME}-{version}-macOS"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copytree(app_path, stage / f"{APP_NAME}.app")
    write_text(
        stage / "README.txt",
        f"""LabGPU {version} for macOS

How to open:

1. Drag LabGPU.app to Applications, or run it from this DMG.
2. Because this alpha build is not Apple notarized yet, macOS may say it cannot
   verify the developer.
3. Use right-click / Control-click -> Open, then choose Open again.

If macOS still blocks the app after download, remove the quarantine attribute:

  xattr -dr com.apple.quarantine /Applications/LabGPU.app

Then open LabGPU.app again.

LabGPU runs locally on 127.0.0.1, reads your normal ~/.ssh/config, and opens
an app-style browser window. It does not install daemons on remote GPU servers.

This package is unsigned/not notarized in alpha. A future release can remove this
warning once it is signed with an Apple Developer ID and notarized by Apple.
""",
    )
    dmg_path = DIST / f"{APP_NAME}-{version}-macOS.dmg"
    dmg_path.unlink(missing_ok=True)
    run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", str(stage), "-ov", "-format", "UDZO", str(dmg_path)])
    print(f"Created {dmg_path}")


def build_windows(pyinstaller: list[str], version: str) -> None:
    run([*pyinstaller_base(pyinstaller), "--onefile", str(SRC)])
    exe_path = BUILD / "pyinstaller-dist" / f"{APP_NAME}.exe"
    if not exe_path.exists():
        raise SystemExit(f"expected executable missing: {exe_path}")
    stage = BUILD / f"{APP_NAME}-{version}-Windows"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copy2(exe_path, stage / f"{APP_NAME}.exe")
    write_text(
        stage / "README-Windows.txt",
        f"""LabGPU {version} for Windows

How to open:

1. Double-click LabGPU.exe.
2. If Windows SmartScreen warns about an unknown publisher, choose More info ->
   Run anyway.
3. If your browser says the page is offline or cannot connect, wait a few
   seconds for LabGPU to finish starting, then refresh the page.

Troubleshooting:

- LabGPU is a local app. It opens http://127.0.0.1:<port> on your own machine.
- If double-click gives no useful error, run this from PowerShell in the ZIP
  folder so the local URL and any error stay visible:

    .\\LabGPU.exe desktop --port 8798

- Then open:

    http://127.0.0.1:8798

- Windows Firewall or security software may ask whether to allow LabGPU to bind
  to localhost. Allow private/local access if prompted.

LabGPU runs locally on 127.0.0.1, reads your normal SSH config/keys, and opens
your browser. It does not install daemons on remote GPU servers.

Requirements: Windows OpenSSH client and reachable SSH GPU servers.
""",
    )
    zip_path = DIST / f"{APP_NAME}-{version}-Windows.zip"
    zip_dir(stage, zip_path)
    print(f"Created {zip_path}")


def build_linux(pyinstaller: list[str], version: str) -> None:
    run([*pyinstaller_base(pyinstaller), "--onefile", str(SRC)])
    binary_path = BUILD / "pyinstaller-dist" / APP_NAME
    if not binary_path.exists():
        raise SystemExit(f"expected binary missing: {binary_path}")
    stage = BUILD / f"{APP_NAME}-{version}-Linux"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copy2(binary_path, stage / APP_NAME)
    write_text(stage / "README-Linux.txt", f"Run ./{APP_NAME} to start LabGPU {version}.\n")
    zip_path = DIST / f"{APP_NAME}-{version}-Linux.zip"
    zip_dir(stage, zip_path)
    print(f"Created {zip_path}")


def write_text(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")


def zip_dir(source: Path, output: Path) -> None:
    output.unlink(missing_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source.parent))


if __name__ == "__main__":
    raise SystemExit(main())
