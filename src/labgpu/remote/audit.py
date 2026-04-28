from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from labgpu.core.paths import labgpu_home
from labgpu.utils.time import now_utc


def audit_dir() -> Path:
    path = labgpu_home() / "audit"
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_audit(event: dict[str, Any]) -> None:
    payload = {"time": now_utc(), **event}
    with (audit_dir() / "actions.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
