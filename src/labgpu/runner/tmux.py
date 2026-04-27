from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from labgpu.core.paths import labgpu_home


def has_tmux() -> bool:
    return bool(shutil.which("tmux"))


def start_tmux(run_id: str, session: str, cwd: Path, env: dict[str, str]) -> None:
    if not has_tmux():
        raise RuntimeError("tmux not found; install tmux or run the wrapper manually for development")
    cmd = [sys.executable, "-m", "labgpu.runner.wrapper", run_id]
    merged_env = {
        "LABGPU_HOME": str(labgpu_home()),
        "PYTHONPATH": _pythonpath(),
        **env,
    }
    bootstrap_log = labgpu_home() / "runs" / run_id / "tmux-bootstrap.log"
    env_prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in merged_env.items())
    shell_cmd = (
        f"cd {shlex.quote(str(cwd))} && "
        f"{env_prefix} {shlex.join(cmd)} > {shlex.quote(str(bootstrap_log))} 2>&1"
    )
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, shell_cmd],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stderr.strip():
        raise RuntimeError(result.stderr.strip() or "tmux failed to start")


def kill_tmux(session: str) -> None:
    if not has_tmux():
        return
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        check=False,
        capture_output=True,
        text=True,
    )


def tmux_session_exists(session: str | None) -> bool:
    if not session or not has_tmux():
        return False
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _pythonpath() -> str:
    src_root = Path(__file__).resolve().parents[2]
    current = os.environ.get("PYTHONPATH")
    return f"{src_root}{os.pathsep}{current}" if current else str(src_root)
