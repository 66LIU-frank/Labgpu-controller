from __future__ import annotations

import getpass
import importlib.util
import os
import platform
import shutil
import sys

from labgpu.core.paths import ensure_home, labgpu_home, runs_dir
from labgpu.runner.tmux import has_tmux


def run(_args) -> int:
    home = ensure_home()
    checks = [
        ("python", sys.version.split()[0], sys.version_info >= (3, 10)),
        ("user", getpass.getuser(), True),
        ("host", platform.node() or "localhost", True),
        ("LABGPU_HOME", str(labgpu_home()), home.exists()),
        ("runs_dir_writable", str(runs_dir()), os.access(runs_dir(), os.W_OK)),
        ("nvidia-smi", shutil.which("nvidia-smi") or "not found", bool(shutil.which("nvidia-smi"))),
        ("tmux", shutil.which("tmux") or "not found", has_tmux()),
        ("shell", os.environ.get("SHELL") or "unknown", True),
        ("psutil", "available" if importlib.util.find_spec("psutil") else "not installed", bool(importlib.util.find_spec("psutil"))),
    ]
    width = max(len(name) for name, _, _ in checks)
    failed = False
    for name, value, ok in checks:
        mark = "OK" if ok else "WARN"
        print(f"{mark:4} {name.ljust(width)}  {value}")
        failed = failed or (name in {"python", "LABGPU_HOME", "runs_dir_writable"} and not ok)
    if not shutil.which("nvidia-smi"):
        print("WARN nvidia-smi not found. labgpu status will not work on real NVIDIA GPUs.")
    if not has_tmux():
        print("WARN tmux not found. labgpu run will not work. Please install tmux.")
    return 1 if failed else 0
