from __future__ import annotations

import os
import platform
import shlex
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

from labgpu.remote.dashboard import serve, split_hosts

MAC_APP_BROWSERS = (
    "Google Chrome",
    "Microsoft Edge",
    "Brave Browser",
    "Chromium",
)


def run(args) -> int:
    install_app = getattr(args, "install_app", None)
    if install_app is not None:
        app_path = install_macos_app(Path(install_app).expanduser() if install_app else None)
        print(f"Installed LabGPU app: {app_path}")
        return 0
    port = args.port or find_free_port(args.host)
    url = f"http://{args.host}:{port}"
    if not args.no_open:
        threading.Thread(target=delayed_open_desktop_window, args=(url, args.browser), daemon=True).start()
    serve(
        host=args.host,
        port=port,
        ssh_config=args.config,
        names=split_hosts(args.hosts),
        pattern=args.pattern,
        timeout=args.timeout,
        open_browser=False,
        allow_actions=args.allow_actions,
        fake_lab=args.fake_lab,
    )
    return 0


def find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def delayed_open_desktop_window(url: str, browser: str) -> None:
    time.sleep(0.5)
    open_desktop_window(url, browser=browser)


def open_desktop_window(url: str, *, browser: str = "auto") -> bool:
    if platform.system() == "Darwin":
        browsers = (browser,) if browser and browser != "auto" else MAC_APP_BROWSERS
        for app_name in browsers:
            if open_macos_app_window(url, app_name):
                return True
    return bool(webbrowser.open(url))


def open_macos_app_window(url: str, app_name: str) -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/open", "-na", app_name, "--args", f"--app={url}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def install_macos_app(target: Path | None = None) -> Path:
    if platform.system() != "Darwin":
        raise RuntimeError("LabGPU.app install is only supported on macOS.")
    app_path = target or default_macos_app_path()
    if app_path.suffix != ".app":
        app_path = app_path / "LabGPU.app"
    contents = app_path / "Contents"
    macos_dir = contents / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_text(app_info_plist(), encoding="utf-8")
    launcher = macos_dir / "LabGPU"
    launcher.write_text(app_launcher_script(), encoding="utf-8")
    launcher.chmod(0o755)
    return app_path


def default_macos_app_path() -> Path:
    system_apps = Path("/Applications")
    if os.access(system_apps, os.W_OK):
        return system_apps / "LabGPU.app"
    return Path.home() / "Applications" / "LabGPU.app"


def app_launcher_script() -> str:
    labgpu_bin = shutil.which("labgpu") or "labgpu"
    path_prefix = "$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return "\n".join(
        [
            "#!/bin/sh",
            f'export PATH="{path_prefix}:$PATH"',
            f"exec {shlex.quote(labgpu_bin)} desktop",
            "",
        ]
    )


def app_info_plist() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>LabGPU</string>
  <key>CFBundleIdentifier</key>
  <string>dev.labgpu.desktop</string>
  <key>CFBundleName</key>
  <string>LabGPU</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>CFBundleVersion</key>
  <string>0.1.0</string>
  <key>LSMinimumSystemVersion</key>
  <string>10.15</string>
</dict>
</plist>
"""
