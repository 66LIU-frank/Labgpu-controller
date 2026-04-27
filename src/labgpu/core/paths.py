from __future__ import annotations

import os
from pathlib import Path


def labgpu_home() -> Path:
    raw = os.environ.get("LABGPU_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".labgpu").resolve()


def runs_dir() -> Path:
    return labgpu_home() / "runs"


def cache_dir() -> Path:
    return labgpu_home() / "cache"


def logs_dir() -> Path:
    return labgpu_home() / "logs"


def ensure_home() -> Path:
    home = labgpu_home()
    runs_dir().mkdir(parents=True, exist_ok=True)
    cache_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    return home
