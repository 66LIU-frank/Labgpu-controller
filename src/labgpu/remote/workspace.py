from __future__ import annotations

from typing import Any


def training_items(hosts: object, overview: dict[str, object]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for host in hosts if isinstance(hosts, list) else []:
        if not isinstance(host, dict):
            continue
        for run in host.get("labgpu_runs") or []:
            if not isinstance(run, dict):
                continue
            items.append(
                {
                    "kind": "run",
                    "name": run.get("name") or "-",
                    "host": host.get("alias"),
                    "gpu": ",".join(str(value) for value in run.get("requested_gpu_indices") or []) or "-",
                    "pid": run.get("pid"),
                    "status": run.get("status") or "unknown",
                    "runtime": run.get("runtime") or "-",
                    "last_log_time": run.get("last_log_time") or run.get("updated_at") or "-",
                    "health": run.get("failure_reason") or run.get("status") or "unknown",
                    "diagnosis": run.get("failure_reason") or "-",
                    "command": run.get("command") or "",
                    "log_path": run.get("log_path"),
                }
            )
    seen: set[tuple[object, object]] = set()
    for proc in overview.get("my_process_items") or []:
        if not isinstance(proc, dict):
            continue
        key = (proc.get("server"), proc.get("pid"))
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "kind": "process",
                "name": proc.get("matched_run_name") or proc.get("tracking_status") or "agentless process",
                "host": proc.get("server"),
                "gpu": proc.get("gpu_index") if proc.get("gpu_index") is not None else proc.get("gpu_uuid"),
                "pid": proc.get("pid"),
                "status": proc.get("tracking_status") or "agentless",
                "runtime": proc.get("runtime") or "-",
                "last_log_time": proc.get("last_log_time") or "-",
                "health": proc.get("health_status") or "unknown",
                "diagnosis": proc.get("health_reason") or "-",
                "command": proc.get("command") or "",
                "log_path": proc.get("log_path"),
                "process": proc,
            }
        )
    return items


def failure_inbox_items(hosts: object, overview: dict[str, object]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for item in training_items(hosts, overview):
        status = str(item.get("status") or "").lower()
        health = str(item.get("health") or "").lower()
        diagnosis = str(item.get("diagnosis") or "").lower()
        if status == "failed" or health not in {"", "healthy", "running", "agentless"} or any(
            word in diagnosis for word in ("oom", "traceback", "nccl", "disk", "killed", "nan")
        ):
            entry = dict(item)
            entry["source"] = "run" if item.get("kind") == "run" else "process"
            items.append(entry)
    for alert in overview.get("alert_items") or []:
        if not isinstance(alert, dict):
            continue
        if alert.get("type") in {"disk_critical", "disk_warning", "suspected_idle", "zombie", "io_wait"}:
            items.append(
                {
                    "kind": "problem",
                    "source": "alert",
                    "name": alert.get("type"),
                    "host": alert.get("server"),
                    "gpu": "-",
                    "pid": "-",
                    "status": alert.get("severity"),
                    "runtime": "-",
                    "health": alert.get("type"),
                    "diagnosis": alert.get("message"),
                    "command": "",
                }
            )
    return items
