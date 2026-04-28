from __future__ import annotations

import json

from labgpu.remote.dashboard import collect_servers, split_hosts
from labgpu.remote.workspace import training_items


def run(args) -> int:
    data = collect_servers(
        ssh_config=args.config,
        names=split_hosts(args.hosts),
        pattern=args.pattern,
        timeout=args.timeout,
        fake_lab=args.fake_lab,
    )
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    rows = [where_row(item) for item in training_items(data.get("hosts") or [], overview)]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if not rows:
        print("No LabGPU run or own GPU process found on selected SSH hosts.")
        return 0
    headers = ["Host", "GPU", "Name / Command", "Status", "PID", "Runtime", "Health"]
    widths = [len(item) for item in headers]
    for row in rows:
        values = [row["host"], row["gpu"], row["name"], row["status"], row["pid"], row["runtime"], row["health"]]
        for index, value in enumerate(values):
            widths[index] = max(widths[index], len(str(value)))
    print("  ".join(headers[index].ljust(widths[index]) for index in range(len(headers))))
    for row in rows:
        values = [row["host"], row["gpu"], row["name"], row["status"], row["pid"], row["runtime"], row["health"]]
        print("  ".join(str(values[index]).ljust(widths[index]) for index in range(len(headers))))
    return 0


def where_row(item: dict[str, object]) -> dict[str, object]:
    name = item.get("name")
    if not name or name in {"agentless process", "untracked", "unknown"}:
        name = item.get("command") or "agentless process"
    return {
        "host": item.get("host") or "-",
        "gpu": item.get("gpu") if item.get("gpu") is not None else "-",
        "name": short(str(name or "-"), 60),
        "status": item.get("status") or "-",
        "pid": item.get("pid") or "-",
        "runtime": item.get("runtime") or "-",
        "health": item.get("health") or "-",
        "diagnosis": item.get("diagnosis") or "-",
    }


def short(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: max(0, width - 3)] + "..."
