from __future__ import annotations

from typing import Any


FREE_MEMORY_USED_THRESHOLD_MB = 512
IDLE_UTIL_THRESHOLD = 3
DISK_WARN_PERCENT = 90
DISK_ERROR_PERCENT = 95


def annotate_servers(hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [annotate_server(host) for host in hosts]


def annotate_server(host: dict[str, Any]) -> dict[str, Any]:
    for gpu in host.get("gpus") or []:
        if not isinstance(gpu, dict):
            continue
        annotate_gpu(gpu)
        for proc in gpu.get("processes") or []:
            if isinstance(proc, dict):
                annotate_process(proc, gpu=gpu)
    for proc in host.get("processes") or []:
        if isinstance(proc, dict):
            gpu = find_gpu(host, proc.get("gpu_uuid"))
            if gpu:
                proc.setdefault("gpu_index", gpu.get("index"))
            annotate_process(proc, gpu=gpu)
            if proc.get("is_current_user"):
                if host.get("shared_account"):
                    proc["actions_disabled_reason"] = "shared account"
                elif not host.get("allow_stop_own_process", True):
                    proc["actions_disabled_reason"] = "disabled by config"
    host["available_gpus"] = available_gpus(host)
    host["my_processes"] = my_processes(host)
    host["alerts"] = alerts_for_server(host)
    if host.get("shared_account"):
        for proc in host.get("my_processes") or []:
            if isinstance(proc, dict):
                proc["actions_disabled_reason"] = "shared account"
    return host


def build_overview(hosts: list[dict[str, Any]]) -> dict[str, Any]:
    online = [host for host in hosts if host.get("online")]
    available: list[dict[str, Any]] = []
    mine: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    total_gpus = 0
    for host in hosts:
        total_gpus += len(host.get("gpus") or [])
        available.extend(host.get("available_gpus") or [])
        mine.extend(host.get("my_processes") or [])
        alerts.extend(host.get("alerts") or [])
    return {
        "online_servers": len(online),
        "total_servers": len(hosts),
        "total_gpus": total_gpus,
        "available_gpus": len(available),
        "my_processes": len(mine),
        "alerts": len(alerts),
        "available_gpu_items": available,
        "my_process_items": mine,
        "alert_items": alerts,
    }


def annotate_gpu(gpu: dict[str, Any]) -> dict[str, Any]:
    processes = [proc for proc in gpu.get("processes") or [] if isinstance(proc, dict)]
    used = gpu.get("memory_used_mb")
    util = gpu.get("utilization_gpu")
    if not processes and (used is None or used <= FREE_MEMORY_USED_THRESHOLD_MB):
        gpu["status"] = "free"
        gpu["health_status"] = "ok"
        gpu["health_reason"] = "No compute processes."
    elif processes and util is not None and util < IDLE_UTIL_THRESHOLD and (used or 0) > 1024:
        gpu["status"] = "possible_idle"
        gpu["health_status"] = "warning"
        gpu["health_reason"] = "GPU memory is occupied while current utilization is low."
    elif processes:
        gpu["status"] = "busy"
        gpu["health_status"] = "ok"
        gpu["health_reason"] = "GPU has active compute processes."
    else:
        gpu["status"] = "unknown"
        gpu["health_status"] = "unknown"
        gpu["health_reason"] = "GPU memory is occupied without visible compute processes."
    return gpu


def annotate_process(proc: dict[str, Any], *, gpu: dict[str, Any] | None = None) -> dict[str, Any]:
    state = str(proc.get("state") or "")
    proc["runtime"] = human_duration(proc.get("runtime_seconds"))
    if state.startswith("Z"):
        proc["health_status"] = "error"
        proc["health_reason"] = "Zombie process."
    elif state.startswith("D"):
        proc["health_status"] = "warning"
        proc["health_reason"] = "Uninterruptible sleep; may be blocked on IO."
    elif gpu and gpu.get("status") == "possible_idle":
        proc["health_status"] = "warning"
        proc["health_reason"] = "Possibly idle: GPU memory is occupied but current GPU utilization is low."
    else:
        proc["health_status"] = "ok"
        proc["health_reason"] = "Running."
    return proc


def available_gpus(host: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for gpu in host.get("gpus") or []:
        if not isinstance(gpu, dict):
            continue
        if gpu.get("status") != "free":
            continue
        items.append(
            {
                "server": host.get("alias"),
                "gpu_index": gpu.get("index"),
                "name": gpu.get("name"),
                "memory_free_mb": gpu.get("memory_free_mb"),
                "memory_total_mb": gpu.get("memory_total_mb"),
                "utilization_gpu": gpu.get("utilization_gpu"),
                "cuda_visible_devices": str(gpu.get("index")),
                "tags": host.get("tags") or [],
            }
        )
    return items


def my_processes(host: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for proc in host.get("processes") or []:
        if not isinstance(proc, dict) or not proc.get("is_current_user"):
            continue
        item = dict(proc)
        item["server"] = host.get("alias")
        item["remote_hostname"] = host.get("remote_hostname")
        if host.get("shared_account"):
            item["actions_disabled_reason"] = "shared account"
        elif not host.get("allow_stop_own_process", True):
            item["actions_disabled_reason"] = "disabled by config"
        items.append(item)
    return items


def alerts_for_server(host: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    alias = host.get("alias")
    if not host.get("online"):
        alerts.append({"server": alias, "severity": "error", "message": host.get("error") or "SSH probe failed."})
        return alerts
    for disk in host.get("disks") or []:
        if not isinstance(disk, dict):
            continue
        usage = parse_percent(disk.get("use_percent"))
        if usage is None:
            continue
        if usage >= DISK_ERROR_PERCENT:
            alerts.append(
                {"server": alias, "severity": "error", "message": f"Disk {disk.get('mount')} is {disk.get('use_percent')} used."}
            )
        elif usage >= DISK_WARN_PERCENT:
            alerts.append(
                {"server": alias, "severity": "warning", "message": f"Disk {disk.get('mount')} is {disk.get('use_percent')} used."}
            )
    for gpu in host.get("gpus") or []:
        if not isinstance(gpu, dict):
            continue
        if gpu.get("status") == "possible_idle":
            alerts.append(
                {
                    "server": alias,
                    "severity": "warning",
                    "message": f"GPU {gpu.get('index')} may be idle while {gpu.get('memory_used_mb')} MB is occupied.",
                }
            )
    return alerts


def find_gpu(host: dict[str, Any], uuid: object) -> dict[str, Any] | None:
    for gpu in host.get("gpus") or []:
        if isinstance(gpu, dict) and gpu.get("uuid") == uuid:
            return gpu
    return None


def parse_percent(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip().rstrip("%")
    try:
        return int(text)
    except ValueError:
        return None


def human_duration(seconds: object) -> str:
    if not isinstance(seconds, int):
        return "-"
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
