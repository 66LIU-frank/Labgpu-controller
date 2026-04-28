from __future__ import annotations

import subprocess
import time
from typing import Any

from labgpu.remote.audit import append_audit
from labgpu.remote.probe import probe_host
from labgpu.remote.ssh_config import SSHHost
from labgpu.remote.state import annotate_server


STOP_SCRIPT = r"""
set -eu
pid="$1"
sig="$2"
if ! kill -0 "$pid" 2>/dev/null; then
  echo "missing"
  exit 0
fi
kill "-$sig" "$pid" 2>/dev/null || {
  echo "kill_failed"
  exit 2
}
sleep 1
if kill -0 "$pid" 2>/dev/null; then
  echo "alive"
else
  echo "stopped"
fi
"""


def stop_process(
    host: SSHHost,
    *,
    pid: int,
    expected_user: str,
    expected_start_time: str | None,
    expected_command_hash: str | None,
    force: bool = False,
    timeout: int = 8,
) -> dict[str, Any]:
    current = annotate_server(probe_host(host, timeout=timeout))
    proc = find_process(current, pid)
    if not proc:
        return finish(host, pid, "not_found", ok=False, message="Process no longer exists.")
    if not proc.get("is_current_user"):
        return finish(host, pid, "not_current_user", ok=False, message="Refusing to stop a process that is not owned by the SSH user.")
    if expected_user and proc.get("user") != expected_user:
        return finish(host, pid, "process_identity_changed", ok=False, message="Process owner changed. Refresh before stopping.")
    if expected_start_time and proc.get("start_time") != expected_start_time:
        return finish(host, pid, "process_identity_changed", ok=False, message="Process start time changed. Refresh before stopping.")
    if expected_command_hash and proc.get("command_hash") != expected_command_hash:
        return finish(host, pid, "process_identity_changed", ok=False, message="Process command changed. Refresh before stopping.")

    signal = "KILL" if force else "TERM"
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", "-q", host.alias, "sh", "-s", "--", str(pid), signal],
            check=False,
            capture_output=True,
            input=STOP_SCRIPT,
            text=True,
            timeout=max(timeout + 4, 6),
        )
    except subprocess.TimeoutExpired:
        return finish(host, pid, "timeout", ok=False, message="Stop command timed out.", signal=signal, proc=proc)
    except OSError as exc:
        return finish(host, pid, "ssh_error", ok=False, message=str(exc), signal=signal, proc=proc)

    status = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "unknown"
    ok = result.returncode == 0 and status in {"stopped", "missing"}
    message = "Process stopped." if ok else f"Stop sent but process is still {status}."
    return finish(
        host,
        pid,
        status,
        ok=ok,
        message=message,
        signal=signal,
        proc=proc,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def find_process(server: dict[str, Any], pid: int) -> dict[str, Any] | None:
    for proc in server.get("processes") or []:
        if isinstance(proc, dict) and proc.get("pid") == pid:
            return proc
    return None


def finish(
    host: SSHHost,
    pid: int,
    result: str,
    *,
    ok: bool,
    message: str,
    signal: str | None = None,
    proc: dict[str, Any] | None = None,
    elapsed_ms: int | None = None,
) -> dict[str, Any]:
    payload = {
        "ok": ok,
        "result": result,
        "message": message,
        "server": host.alias,
        "pid": pid,
        "signal": signal,
        "elapsed_ms": elapsed_ms,
    }
    append_audit(
        {
            "action": "stop_process",
            "server": host.alias,
            "pid": pid,
            "user": proc.get("user") if proc else None,
            "command": proc.get("command") if proc else None,
            "signal": signal,
            "result": result,
            "ok": ok,
        }
    )
    return payload
