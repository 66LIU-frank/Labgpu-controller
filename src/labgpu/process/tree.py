from __future__ import annotations

import os
import signal
import subprocess

from labgpu.process.inspector import pid_exists


def terminate_process_tree(pid: int, *, force: bool = False) -> None:
    sig = signal.SIGKILL if force else signal.SIGTERM
    children = child_pids(pid)
    for child in reversed(children):
        _kill_one(child, sig)
    try:
        os.killpg(pid, sig)
    except OSError:
        _kill_one(pid, sig)


def _kill_one(pid: int, sig: signal.Signals) -> None:
    if not pid_exists(pid):
        return
    os.kill(pid, sig)


def process_tree_pids(pid: int) -> list[int]:
    return [pid, *child_pids(pid)]


def child_pids(pid: int) -> list[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    children = [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]
    nested: list[int] = []
    for child in children:
        nested.extend(child_pids(child))
    return [*children, *nested]
