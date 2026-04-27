from __future__ import annotations

import getpass
import os

from labgpu.cli.resolve import resolve_run_strict
from labgpu.core.store import RunStore
from labgpu.process.inspector import inspect_process, pid_exists
from labgpu.process.tree import terminate_process_tree
from labgpu.runner.tmux import kill_tmux
from labgpu.utils.time import now_utc


def run(args) -> int:
    store = RunStore()
    meta = resolve_run_strict(store, args.ref, action="kill")
    if meta.user != getpass.getuser() and os.geteuid() != 0:
        raise PermissionError("refusing to kill another user's experiment")
    if meta.pid is None and not meta.tmux_session:
        raise RuntimeError("run has no pid or tmux session")
    if meta.pid is not None and pid_exists(meta.pid):
        info = inspect_process(meta.pid)
        owner = info.get("user") or info.get("username")
        if owner and owner != getpass.getuser() and os.geteuid() != 0:
            raise PermissionError(f"refusing to kill pid {meta.pid} owned by {owner}")
        terminate_process_tree(meta.pid, force=args.force)
    if meta.tmux_session:
        kill_tmux(meta.tmux_session)
    store.update(
        meta.run_id,
        status="killed",
        ended_at=now_utc(),
        failure_reason="Killed by user",
    )
    print(f"Killed: {meta.run_id}")
    return 0
