from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from labgpu.utils.time import now_utc


def append_event(run_dir: Path, event_type: str, **payload: Any) -> None:
    event = {"time": now_utc(), "type": event_type, **payload}
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"type": "corrupt_event", "raw": line})
    return events
