from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
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

SAFE_SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


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
    if host.shared_account:
        return finish(host, pid, "shared_account_disabled", ok=False, message="Stop is disabled for shared-account servers in Agentless Mode.")
    if not host.allow_stop_own_process:
        return finish(host, pid, "actions_disabled", ok=False, message="Stop is disabled for this server by config.")
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


def open_ssh_terminal(host: SSHHost) -> dict[str, Any]:
    alias = host.alias
    if not is_safe_ssh_alias(alias):
        return {
            "ok": False,
            "result": "invalid_alias",
            "message": "Refusing to open an SSH terminal for an unsafe alias.",
            "server": alias,
        }
    command = f"ssh {alias}"
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "Terminal" to activate',
                    "-e",
                    f'tell application "Terminal" to do script {json.dumps(command)}',
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
            if result.returncode != 0:
                return terminal_result(host, "open_failed", ok=False, message=result.stderr.strip() or "Opening Terminal failed.", command=command)
        elif sys.platform.startswith("win"):
            subprocess.Popen(["cmd", "/c", "start", "LabGPU SSH", "cmd", "/k", "ssh", alias])
        else:
            launcher = linux_terminal_launcher(alias)
            if not launcher:
                return terminal_result(host, "no_terminal", ok=False, message="No supported terminal emulator found.", command=command)
            subprocess.Popen(launcher)
    except subprocess.TimeoutExpired:
        return terminal_result(host, "timeout", ok=False, message="Opening terminal timed out.", command=command)
    except OSError as exc:
        return terminal_result(host, "open_error", ok=False, message=str(exc), command=command)

    return terminal_result(host, "opened", ok=True, message=f"Opening SSH terminal for {alias}.", command=command)


def linux_terminal_launcher(alias: str) -> list[str] | None:
    candidates = [
        ("x-terminal-emulator", ["x-terminal-emulator", "-e", "ssh", alias]),
        ("gnome-terminal", ["gnome-terminal", "--", "ssh", alias]),
        ("konsole", ["konsole", "-e", "ssh", alias]),
        ("xterm", ["xterm", "-e", "ssh", alias]),
    ]
    for binary, command in candidates:
        if shutil.which(binary):
            return command
    return None


def is_safe_ssh_alias(alias: str) -> bool:
    return bool(alias and not alias.startswith("-") and SAFE_SSH_ALIAS_RE.fullmatch(alias))


def terminal_result(host: SSHHost, result: str, *, ok: bool, message: str, command: str) -> dict[str, Any]:
    payload = {
        "ok": ok,
        "result": result,
        "message": message,
        "server": host.alias,
        "command": command,
    }
    append_audit(
        {
            "action": "open_ssh_terminal",
            "server": host.alias,
            "result": result,
            "ok": ok,
        }
    )
    return payload
