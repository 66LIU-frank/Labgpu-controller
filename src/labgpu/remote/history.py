from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path
from typing import Any

from labgpu.core.paths import cache_dir
from labgpu.remote.cache import safe_alias
from labgpu.utils.time import now_utc, parse_time

MIN_IDLE_OCCUPIED_MB = 1024


def history_dir() -> Path:
    path = cache_dir() / "history"
    path.mkdir(parents=True, exist_ok=True)
    return path


def history_path(alias: str) -> Path:
    return history_dir() / f"{safe_alias(alias)}.jsonl"


def append_history(server: dict[str, Any], *, limit: int = 120) -> None:
    alias = str(server.get("alias") or "")
    if not alias or not server.get("online"):
        return
    path = history_path(alias)
    rows = read_history(alias)[-limit + 1 :]
    rows.append(compact_snapshot(server))
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_history(alias: str) -> list[dict[str, Any]]:
    path = history_path(alias)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    except (OSError, json.JSONDecodeError):
        return []
    return rows


def compact_snapshot(server: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": server.get("probed_at") or now_utc(),
        "gpus": [
            {
                "index": gpu.get("index"),
                "uuid": gpu.get("uuid"),
                "utilization_gpu": gpu.get("utilization_gpu"),
                "memory_used_mb": gpu.get("memory_used_mb"),
            }
            for gpu in server.get("gpus") or []
            if isinstance(gpu, dict)
        ],
        "processes": [
            {
                "pid": proc.get("pid"),
                "gpu_uuid": proc.get("gpu_uuid"),
                "cpu_percent": proc.get("cpu_percent"),
                "used_memory_mb": proc.get("used_memory_mb"),
            }
            for proc in server.get("processes") or []
            if isinstance(proc, dict)
        ],
    }


def apply_history_evidence(server: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    if not history:
        return server
    recent = history[-6:]
    gpu_history = index_gpu_history(recent)
    proc_history = index_proc_history(recent)
    for gpu in server.get("gpus") or []:
        if not isinstance(gpu, dict):
            continue
        evidence = gpu_idle_evidence(gpu, gpu_history.get(str(gpu.get("uuid"))) or [])
        if evidence:
            gpu["status"] = "possible_idle"
            gpu["availability"] = "idle_but_occupied"
            gpu["health_status"] = "suspected_idle"
            gpu["health_severity"] = "warning"
            gpu["idle_evidence"] = evidence
            gpu["confidence"] = evidence["confidence"]
            gpu["health_reason"] = evidence["summary"]
    for proc in server.get("processes") or []:
        if not isinstance(proc, dict):
            continue
        gpu = find_gpu(server, proc.get("gpu_uuid"))
        gpu_evidence = gpu.get("idle_evidence") if isinstance(gpu, dict) else None
        if proc.get("health_status") == "suspected_idle" and isinstance(gpu_evidence, dict):
            proc["idle_evidence"] = gpu_evidence
            proc["confidence"] = gpu_evidence["confidence"]
            proc["health_reason"] = gpu_evidence["summary"]
        proc_rows = proc_history.get(str(proc.get("pid"))) or []
        if proc_rows:
            low_cpu = sum(1 for row in proc_rows if float(row.get("cpu_percent") or 0) < 2)
            proc["cpu_low_samples"] = low_cpu
    return server


def index_gpu_history(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for gpu in row.get("gpus") or []:
            if isinstance(gpu, dict) and gpu.get("uuid"):
                item = dict(gpu)
                item["_time"] = row.get("time")
                indexed.setdefault(str(gpu["uuid"]), []).append(item)
    return indexed


def index_proc_history(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for proc in row.get("processes") or []:
            if isinstance(proc, dict) and proc.get("pid") is not None:
                indexed.setdefault(str(proc["pid"]), []).append(proc)
    return indexed


def gpu_idle_evidence(gpu: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(rows) < 2:
        return None
    used_mb = int(gpu.get("memory_used_mb") or 0)
    if used_mb <= MIN_IDLE_OCCUPIED_MB:
        return None
    low_util = [row for row in rows if int(row.get("utilization_gpu") or 0) < 3]
    occupied = [row for row in rows if int(row.get("memory_used_mb") or 0) > MIN_IDLE_OCCUPIED_MB]
    if len(low_util) < 2 or len(occupied) < 2:
        return None
    confidence = "high" if len(low_util) >= 5 and len(occupied) >= 5 else "medium"
    elapsed_seconds = history_elapsed_seconds(rows)
    span = format_elapsed(elapsed_seconds) if elapsed_seconds is not None else f"{len(rows)} samples"
    sample_note = f"based on {len(rows)} samples"
    return {
        "confidence": confidence,
        "low_util_samples": len(low_util),
        "occupied_samples": len(occupied),
        "vram_occupied_mb": used_mb,
        "sample_count": len(rows),
        "elapsed_seconds": elapsed_seconds,
        "summary": f"GPU util < 3% over {span} ({sample_note}) while {used_mb} MB VRAM is occupied.",
    }


def history_elapsed_seconds(rows: list[dict[str, Any]]) -> int | None:
    timestamps = [parse_time(str(row.get("_time") or "")) for row in rows]
    timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    if len(timestamps) < 2:
        return None
    timestamps = [timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc) for timestamp in timestamps]
    first = min(timestamps)
    last = max(timestamps)
    return max(0, int((last - first).total_seconds()))


def format_elapsed(seconds: int | None) -> str:
    if seconds is None:
        return "unknown time"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def find_gpu(server: dict[str, Any], uuid: object) -> dict[str, Any] | None:
    for gpu in server.get("gpus") or []:
        if isinstance(gpu, dict) and gpu.get("uuid") == uuid:
            return gpu
    return None
