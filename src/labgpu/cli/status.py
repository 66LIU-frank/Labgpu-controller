from __future__ import annotations

import json
import time
from typing import Any

from labgpu.core.refresh import refresh_runs
from labgpu.core.store import RunStore
from labgpu.gpu.fake import FakeCollector
from labgpu.gpu.nvidia_smi import NvidiaSmiCollector
from labgpu.utils.shell import short_command


def run(args) -> int:
    while True:
        payload = collect_status(fake=args.fake)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print_status(payload)
        if not args.watch:
            return 0
        time.sleep(args.interval)


def collect_status(*, fake: bool = False) -> dict[str, Any]:
    collector = FakeCollector() if fake else NvidiaSmiCollector()
    gpu = collector.collect()
    store = RunStore()
    refresh_runs(store)
    tracked = store.running_by_pid()
    for device in gpu.get("gpus", []):
        processes = device.get("processes", [])
        if not processes:
            device["labgpu_state"] = "free"
            continue
        state = "untracked"
        for proc in processes:
            meta = tracked.get(int(proc.get("pid", -1)))
            if meta:
                proc["experiment"] = {"run_id": meta.run_id, "name": meta.name, "status": meta.status}
                proc["matched_run_id"] = meta.run_id
                proc["matched_run_name"] = meta.name
                proc["tracking_status"] = "adopted" if meta.launch_mode == "adopted" else "tracked"
                state = proc["tracking_status"]
            elif not proc.get("user"):
                proc["matched_run_id"] = None
                proc["matched_run_name"] = None
                proc["tracking_status"] = "unknown"
                state = "unknown"
            else:
                proc["matched_run_id"] = None
                proc["matched_run_name"] = None
                proc["tracking_status"] = "untracked"
        device["labgpu_state"] = state
    return {"gpu": gpu}


def print_status(payload: dict[str, Any]) -> None:
    gpu = payload["gpu"]
    if not gpu.get("available"):
        print(f"GPU status unavailable: {gpu.get('error')}")
        return
    host = gpu.get("host")
    if host:
        print(f"Host: {host}")
    header = ["GPU", "Name", "Memory", "Util", "Temp", "User", "PID", "Experiment", "Command"]
    rows = []
    for device in gpu.get("gpus", []):
        processes = device.get("processes", [])
        if not processes:
            rows.append(_row(device, "-", "-", "free", "-"))
            continue
        for proc in processes:
            exp = proc.get("experiment") or {}
            name = exp.get("name") or "untracked"
            rows.append(_row(device, proc.get("user") or proc.get("username") or "unknown", proc.get("pid") or "-", name, proc.get("command") or proc.get("cmdline") or "-"))
    print_table(header, rows)


def _row(device: dict[str, Any], user: object, pid: object, experiment: object, command: object) -> list[str]:
    total = device.get("memory_total_mb")
    used = device.get("memory_used_mb")
    util = device.get("utilization_gpu")
    temp = device.get("temperature")
    memory = f"{used}/{total}MB" if total is not None else "-"
    return [
        str(device.get("index", "-")),
        short_command(str(device.get("name", "-")), 18),
        memory,
        f"{util}%" if util is not None else "-",
        f"{temp}C" if temp is not None else "-",
        str(user),
        str(pid),
        short_command(str(experiment), 24),
        short_command(str(command), 36),
    ]


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(head) for head in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
