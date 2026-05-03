from __future__ import annotations

import platform
import socket
import subprocess
import threading
import time
import webbrowser

from labgpu.remote.dashboard import serve, split_hosts

MAC_APP_BROWSERS = (
    "Google Chrome",
    "Microsoft Edge",
    "Brave Browser",
    "Chromium",
)


def run(args) -> int:
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
