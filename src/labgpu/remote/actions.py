from __future__ import annotations

import json
import random
import re
import shlex
import shutil
import socket
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
SUPPORTED_TERMINAL_AGENTS = {"none", "codex", "claude", "gemini", "openclaw"}


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


def open_ssh_terminal(
    host: SSHHost,
    *,
    proxy_port: str | int | None = None,
    local_proxy_port: str | int | None = None,
    remote_proxy_port: str | int | None = None,
    agent: str = "none",
) -> dict[str, Any]:
    alias = host.alias
    if not is_safe_ssh_alias(alias):
        return {
            "ok": False,
            "result": "invalid_alias",
            "message": "Refusing to open an SSH terminal for an unsafe alias.",
            "server": alias,
        }
    try:
        local_port, _remote_port = normalize_proxy_ports(
            proxy_port=proxy_port,
            local_proxy_port=local_proxy_port,
            remote_proxy_port=remote_proxy_port,
        )
        if local_port and not is_local_tcp_port_open(local_port):
            return {
                "ok": False,
                "result": "local_proxy_not_listening",
                "message": f"Local proxy port 127.0.0.1:{local_port} is not listening.",
                "server": alias,
            }
        argv = build_ssh_terminal_argv(
            alias,
            proxy_port=proxy_port,
            local_proxy_port=local_proxy_port,
            remote_proxy_port=remote_proxy_port,
            agent=agent,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "result": "invalid_terminal_options",
            "message": str(exc),
            "server": alias,
        }
    command = shlex.join(argv)
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
            subprocess.Popen(["cmd", "/c", "start", "LabGPU SSH", "cmd", "/k", *argv])
        else:
            launcher = linux_terminal_launcher(argv)
            if not launcher:
                return terminal_result(host, "no_terminal", ok=False, message="No supported terminal emulator found.", command=command)
            subprocess.Popen(launcher)
    except subprocess.TimeoutExpired:
        return terminal_result(host, "timeout", ok=False, message="Opening terminal timed out.", command=command)
    except OSError as exc:
        return terminal_result(host, "open_error", ok=False, message=str(exc), command=command)

    return terminal_result(host, "opened", ok=True, message=f"Opening SSH terminal for {alias}.", command=command)


def build_ssh_terminal_argv(
    alias: str,
    *,
    proxy_port: str | int | None = None,
    local_proxy_port: str | int | None = None,
    remote_proxy_port: str | int | None = None,
    agent: str = "none",
) -> list[str]:
    if not is_safe_ssh_alias(alias):
        raise ValueError("Unsafe SSH alias.")
    normalized_agent = normalize_terminal_agent(agent)
    local_port, remote_port = normalize_proxy_ports(
        proxy_port=proxy_port,
        local_proxy_port=local_proxy_port,
        remote_proxy_port=remote_proxy_port,
    )
    remote_command = terminal_remote_command(remote_port, normalized_agent, local_proxy_port=local_port)
    argv = ["ssh"]
    if local_port and remote_port:
        argv.extend(["-o", "ExitOnForwardFailure=yes", "-R", f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}"])
    if remote_command:
        argv.extend(["-t", alias, remote_command])
    else:
        argv.append(alias)
    return argv


def normalize_terminal_agent(value: str | None) -> str:
    agent = str(value or "none").strip().lower()
    aliases = {
        "claudecode": "claude",
        "claude-code": "claude",
        "gemini-cli": "gemini",
        "open-claw": "openclaw",
        "claw": "openclaw",
    }
    agent = aliases.get(agent, agent)
    if agent not in SUPPORTED_TERMINAL_AGENTS:
        raise ValueError("Unsupported terminal launcher.")
    return agent


def normalize_proxy_port(value: str | int | None) -> int | None:
    if value in {None, "", "none", "false", "0"}:
        return None
    try:
        port = int(str(value).strip())
    except ValueError as exc:
        raise ValueError("Proxy port must be a number.") from exc
    if port < 1 or port > 65535:
        raise ValueError("Proxy port must be between 1 and 65535.")
    return port


def normalize_proxy_ports(
    *,
    proxy_port: str | int | None = None,
    local_proxy_port: str | int | None = None,
    remote_proxy_port: str | int | None = None,
) -> tuple[int | None, int | None]:
    legacy_value = str(proxy_port or "").strip()
    local_explicit = str(local_proxy_port or "").strip()
    remote_explicit = str(remote_proxy_port or "").strip()
    local_value = local_explicit or legacy_value
    local_port = normalize_proxy_port(local_value)
    if remote_explicit:
        remote_port = normalize_proxy_port(remote_explicit)
    elif legacy_value and not local_explicit:
        remote_port = normalize_proxy_port(legacy_value)
    elif local_port:
        remote_port = choose_remote_proxy_port()
    else:
        remote_port = None
    if remote_port and not local_port:
        raise ValueError("Local proxy port is required when remote tunnel port is set.")
    return local_port, remote_port


def choose_remote_proxy_port() -> int:
    return random.randint(41000, 60999)


def terminal_remote_command(proxy_port: int | None, agent: str, *, local_proxy_port: int | None = None) -> str:
    parts: list[str] = []
    if proxy_port:
        proxy_url = f"http://127.0.0.1:{proxy_port}"
        parts.append(f"export HTTP_PROXY={shlex.quote(proxy_url)} HTTPS_PROXY={shlex.quote(proxy_url)}")
        local_text = f"127.0.0.1:{local_proxy_port or proxy_port}"
        parts.append(f"echo 'LabGPU proxy: remote HTTP_PROXY/HTTPS_PROXY -> local {local_text}'")
    if agent != "none":
        parts.append(agent_launcher_command(agent))
    elif proxy_port:
        parts.append('if [ -n "${SHELL:-}" ]; then exec "$SHELL" -l; fi; exec /bin/sh')
    return "; ".join(parts)


def agent_launcher_command(agent: str) -> str:
    launchers = {
        "codex": ('command -v codex >/dev/null 2>&1', "codex", "Codex CLI was not found."),
        "claude": (
            "command -v claude >/dev/null 2>&1 || command -v claude-code >/dev/null 2>&1",
            'if command -v claude >/dev/null 2>&1; then claude; else claude-code; fi',
            "Claude Code CLI was not found.",
        ),
        "gemini": ('command -v gemini >/dev/null 2>&1', "gemini", "Gemini CLI was not found."),
        "openclaw": ('command -v openclaw >/dev/null 2>&1', "openclaw agent", "OpenClaw CLI was not found."),
    }
    if agent not in launchers:
        raise ValueError("Unsupported terminal launcher.")
    check, command, missing = launchers[agent]
    script = f'if {check}; then {command}; else echo "{missing}"; fi; exec ${{SHELL:-/bin/sh}} -il'
    return f"exec ${{SHELL:-/bin/sh}} -ilc {shlex.quote(script)}"


def is_local_tcp_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def linux_terminal_launcher(argv: list[str]) -> list[str] | None:
    candidates = [
        ("x-terminal-emulator", ["x-terminal-emulator", "-e", *argv]),
        ("gnome-terminal", ["gnome-terminal", "--", *argv]),
        ("konsole", ["konsole", "-e", *argv]),
        ("xterm", ["xterm", "-e", *argv]),
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
