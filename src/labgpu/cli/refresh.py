from __future__ import annotations

from labgpu.core.refresh import refresh_runs
from labgpu.core.store import RunStore


def run(_args) -> int:
    result = refresh_runs(RunStore())
    print(f"checked={result.checked} updated={result.updated}")
    for run_id in result.orphaned or []:
        print(f"orphaned: {run_id}")
    return 0
