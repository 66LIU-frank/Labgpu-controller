from __future__ import annotations

import json

from labgpu.core.refresh import refresh_runs
from labgpu.core.store import RunStore
from labgpu.utils.time import human_duration


def run(args) -> int:
    store = RunStore()
    refresh_runs(store)
    runs = store.list(all_runs=args.all, user=args.user, status=args.status)
    runs = runs[: args.limit]
    if args.json:
        print(json.dumps([item.to_dict() for item in runs], indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if not runs:
        print("No experiments.")
        return 0
    headers = ["Name", "Run ID", "Status", "User", "Host", "GPU", "Duration", "Exit", "Reason", "Started"]
    rows = [
        [
            meta.name,
            meta.run_id,
            meta.status,
            meta.user,
            meta.host,
            meta.cuda_visible_devices or ",".join(meta.requested_gpu_indices) or "-",
            human_duration(meta.duration_seconds),
            "-" if meta.exit_code is None else str(meta.exit_code),
            meta.failure_reason or "-",
            meta.started_at or meta.created_at,
        ]
        for meta in runs
    ]
    _print_table(headers, rows)
    return 0


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(item) for item in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    for row in rows:
        print("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers))))
