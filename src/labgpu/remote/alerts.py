from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from labgpu.core.paths import cache_dir
from labgpu.utils.time import now_utc


def alerts_state_path() -> Path:
    path = cache_dir() / "alerts_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def alert_key(alert: dict[str, Any]) -> str:
    raw = "\n".join(
        [
            str(alert.get("server") or ""),
            str(alert.get("type") or ""),
            str(alert.get("message") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def load_alert_state(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    target = Path(path) if path else alerts_state_path()
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_alert_state(state: dict[str, dict[str, Any]], path: str | Path | None = None) -> None:
    target = Path(path) if path else alerts_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)


def apply_alert_state(
    alerts: list[dict[str, Any]],
    *,
    path: str | Path | None = None,
    scoped_servers: set[str] | None = None,
) -> list[dict[str, Any]]:
    state = load_alert_state(path)
    now = now_utc()
    current_keys: set[str] = set()
    enriched: list[dict[str, Any]] = []
    for alert in alerts:
        item = dict(alert)
        key = str(item.get("key") or alert_key(item))
        current_keys.add(key)
        record = state.get(key) or {}
        if record.get("status") in {"dismissed", "snoozed"}:
            status = record["status"]
        else:
            status = "active"
        first_seen = record.get("first_seen") or now
        record.update(
            {
                "key": key,
                "server": item.get("server"),
                "type": item.get("type"),
                "severity": item.get("severity"),
                "message": item.get("message"),
                "first_seen": first_seen,
                "last_seen": now,
                "status": status,
            }
        )
        record.pop("resolved_at", None)
        state[key] = record
        item.update(record)
        enriched.append(item)

    for key, record in list(state.items()):
        if key in current_keys:
            continue
        if scoped_servers is not None and str(record.get("server") or "") not in scoped_servers:
            continue
        if record.get("status") != "resolved":
            record["status"] = "resolved"
            record["resolved_at"] = now
            record["last_seen"] = record.get("last_seen") or now
            state[key] = record

    write_alert_state(state, path)
    return enriched


def all_alert_records(path: str | Path | None = None) -> list[dict[str, Any]]:
    state = load_alert_state(path)
    records = [dict(record) for record in state.values() if isinstance(record, dict)]
    records.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
    return records


def set_alert_status(key: str, status: str, *, path: str | Path | None = None) -> dict[str, Any]:
    if status not in {"active", "dismissed", "snoozed", "resolved"}:
        raise ValueError("invalid alert status")
    state = load_alert_state(path)
    record = state.get(key)
    if not record:
        raise KeyError(key)
    record["status"] = status
    record["updated_at"] = now_utc()
    state[key] = record
    write_alert_state(state, path)
    return dict(record)
