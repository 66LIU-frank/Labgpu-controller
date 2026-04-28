from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from labgpu.core.paths import cache_dir


def server_cache_dir() -> Path:
    path = cache_dir() / "servers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def server_cache_path(alias: str) -> Path:
    return server_cache_dir() / f"{safe_alias(alias)}.json"


def read_server_cache(alias: str) -> dict[str, Any] | None:
    path = server_cache_path(alias)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_server_cache(payload: dict[str, Any]) -> None:
    alias = str(payload.get("alias") or "")
    if not alias or not payload.get("online"):
        return
    path = server_cache_path(alias)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def safe_alias(alias: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", alias).strip("._")
    return text or "server"
