from __future__ import annotations

from dataclasses import dataclass

from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore
from labgpu.process.inspector import pid_exists
from labgpu.runner.tmux import tmux_session_exists
from labgpu.utils.time import now_utc


@dataclass
class RefreshResult:
    checked: int = 0
    updated: int = 0
    orphaned: list[str] | None = None


def refresh_runs(store: RunStore | None = None) -> RefreshResult:
    store = store or RunStore()
    result = RefreshResult(orphaned=[])
    for meta in store.list(all_runs=True, status="running"):
        result.checked += 1
        if is_still_running(meta):
            continue
        store.update(
            meta.run_id,
            status="orphaned",
            ended_at=now_utc(),
            failure_reason="Process and tmux session are no longer running",
        )
        result.updated += 1
        assert result.orphaned is not None
        result.orphaned.append(meta.run_id)
    return result


def is_still_running(meta: RunMeta) -> bool:
    if meta.pid is not None and pid_exists(int(meta.pid)):
        return True
    if tmux_session_exists(meta.tmux_session):
        return True
    return False
