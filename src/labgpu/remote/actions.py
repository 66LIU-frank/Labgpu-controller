from __future__ import annotations

import json
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from labgpu.remote.ai_gateway import AIGatewaySession, start_ai_gateway
from labgpu.remote.ai_session import AI_APP_LABELS, DEFAULT_AI_PATH_PREFIXES, EnterServerAIRequest, SUPPORTED_AI_APPS, build_ai_ssh_command, build_network_proxy_url, build_path_export, normalized_remote_command_path, normalized_remote_cwd, normalized_remote_path_prefixes
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
AI_SESSION_TOKEN_RE = re.compile(r"labgpu-session-[A-Za-z0-9_-]{24,}")
SUPPORTED_TERMINAL_AGENTS = {"none", "codex", "claude", "gemini", "openclaw"}
AI_PROXY_TUNNEL_AGENTS = set(SUPPORTED_AI_APPS)
AI_SESSION_MODES = {"proxy_tunnel", "remote_write"}
AI_GATEWAY_SESSIONS: list[AIGatewaySession] = []


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
    ai_mode: str | None = None,
    provider_name: str | None = None,
    gpu_index: str | int | None = None,
    remote_cwd: str | None = None,
    network_proxy_enabled: bool = False,
    network_local_proxy_port: str | int | None = None,
    network_remote_proxy_port: str | int | None = None,
    network_proxy_scheme: str = "http",
) -> dict[str, Any]:
    alias = host.alias
    if not is_safe_ssh_alias(alias):
        return {
            "ok": False,
            "result": "invalid_alias",
            "message": "Refusing to open an SSH terminal for an unsafe alias.",
            "server": alias,
        }
    gateway: AIGatewaySession | None = None
    ccswitch_proxy_port: int | None = None
    remote_gateway_port: int | None = None
    network_local_port: int | None = None
    network_remote_port: int | None = None
    try:
        normalized_agent = normalize_terminal_agent(agent)
        if ai_mode in AI_SESSION_MODES and normalized_agent not in AI_PROXY_TUNNEL_AGENTS:
            raise ValueError("Only Claude Code and Codex CLI AI sessions are available in this alpha.")
        ccswitch_proxy_port, remote_gateway_port = normalize_proxy_ports(
            proxy_port=proxy_port,
            local_proxy_port=local_proxy_port,
            remote_proxy_port=remote_proxy_port,
        )
        port_state = is_local_tcp_port_open(ccswitch_proxy_port) if ccswitch_proxy_port else True
        if ccswitch_proxy_port and port_state is False:
            message = f"Local proxy port 127.0.0.1:{ccswitch_proxy_port} is not listening."
            if normalized_agent in AI_PROXY_TUNNEL_AGENTS and ai_mode in AI_SESSION_MODES:
                message = f"CC Switch {ai_agent_label(normalized_agent)} proxy is configured but not listening on 127.0.0.1:{ccswitch_proxy_port}."
            return {
                "ok": False,
                "result": "local_proxy_not_listening",
                "message": message,
                "server": alias,
            }
        if network_proxy_enabled:
            network_local_port, network_remote_port = normalize_proxy_ports(
                local_proxy_port=network_local_proxy_port,
                remote_proxy_port=network_remote_proxy_port,
            )
            if not network_local_port:
                raise ValueError("Network Tunnel requires a local proxy port.")
            network_port_state = is_local_tcp_port_open(network_local_port)
            if network_port_state is False:
                return {
                    "ok": False,
                    "result": "network_proxy_not_listening",
                    "message": f"Local network proxy port 127.0.0.1:{network_local_port} is not listening.",
                    "server": alias,
                }
        if normalized_agent in AI_PROXY_TUNNEL_AGENTS and ai_mode in AI_SESSION_MODES:
            if not ccswitch_proxy_port:
                raise ValueError(f"{ai_agent_label(normalized_agent)} AI session requires a CC Switch proxy port.")
            gateway = start_ai_gateway(
                target_port=ccswitch_proxy_port,
                metadata={
                    "mode": ai_mode,
                    "app": normalized_agent,
                    "provider": provider_name or "",
                    "server": alias,
                    "remote_cwd": remote_cwd or "",
                    "ccswitch_proxy_port": ccswitch_proxy_port,
                },
            )
            AI_GATEWAY_SESSIONS.append(gateway)
        argv = build_ssh_terminal_argv(
            alias,
            host=host,
            proxy_port=proxy_port,
            local_proxy_port=ccswitch_proxy_port,
            remote_proxy_port=remote_gateway_port,
            agent=normalized_agent,
            ai_mode=ai_mode,
            provider_name=provider_name,
            gpu_index=gpu_index,
            remote_cwd=remote_cwd,
            local_gateway_port=gateway.listen_port if gateway else None,
            session_token=gateway.token if gateway else None,
            network_local_proxy_port=network_local_port,
            network_remote_proxy_port=network_remote_port,
            network_proxy_scheme=network_proxy_scheme,
        )
    except ValueError as exc:
        if gateway:
            close_gateway_session(gateway)
        return {
            "ok": False,
            "result": "invalid_terminal_options",
            "message": str(exc),
            "server": alias,
        }
    except OSError as exc:
        if gateway:
            close_gateway_session(gateway)
        return {
            "ok": False,
            "result": "ai_gateway_start_failed",
            "message": f"Could not start local AI gateway: {exc}",
            "server": alias,
        }
    command = shlex.join(argv)
    redacted_command = redact_ai_session_tokens(command)
    launch_command = command
    launch_script: Path | None = None
    if sys.platform == "darwin" and should_use_terminal_launch_script(command):
        launch_script = write_terminal_launch_script(command)
        launch_command = f"exec /bin/sh {shlex.quote(str(launch_script))}"
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "Terminal" to activate',
                    "-e",
                    f'tell application "Terminal" to do script {json.dumps(launch_command)}',
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
            if result.returncode != 0:
                cleanup_terminal_launch_script(launch_script)
                if gateway:
                    close_gateway_session(gateway)
                return terminal_result(host, "open_failed", ok=False, message=result.stderr.strip() or "Opening Terminal failed.", command=redacted_command)
        elif sys.platform.startswith("win"):
            subprocess.Popen(["cmd", "/c", "start", "LabGPU SSH", "cmd", "/k", *argv])
        else:
            launcher = linux_terminal_launcher(argv)
            if not launcher:
                if gateway:
                    close_gateway_session(gateway)
                return terminal_result(host, "no_terminal", ok=False, message="No supported terminal emulator found.", command=redacted_command)
            subprocess.Popen(launcher)
    except subprocess.TimeoutExpired:
        cleanup_terminal_launch_script(launch_script)
        if gateway:
            close_gateway_session(gateway)
        return terminal_result(host, "timeout", ok=False, message="Opening terminal timed out.", command=redacted_command)
    except OSError as exc:
        cleanup_terminal_launch_script(launch_script)
        if gateway:
            close_gateway_session(gateway)
        return terminal_result(host, "open_error", ok=False, message=str(exc), command=redacted_command)

    message = f"Opening SSH terminal for {alias}."
    if normalize_terminal_agent(agent) in AI_PROXY_TUNNEL_AGENTS and ai_mode in AI_SESSION_MODES and remote_gateway_port:
        message = (
            f"Opening SSH terminal for {alias}. If SSH reports remote port forwarding failed, "
            f"remote gateway port {remote_gateway_port} may already be in use on this server."
        )
    if network_remote_port:
        message += f" Network Tunnel: remote 127.0.0.1:{network_remote_port} -> local proxy 127.0.0.1:{network_local_port}."
    payload = terminal_result(host, "opened", ok=True, message=message, command=redacted_command)
    if gateway and ccswitch_proxy_port and remote_gateway_port:
        payload["ai_gateway"] = {
            "local_gateway_port": gateway.listen_port,
            "remote_gateway_port": remote_gateway_port,
            "ccswitch_proxy_port": ccswitch_proxy_port,
            "token_fingerprint": gateway.token_fingerprint,
        }
        cwd = normalized_remote_cwd(remote_cwd)
        if cwd is not None:
            payload["ai_gateway"]["remote_cwd"] = cwd
    if network_local_port and network_remote_port:
        payload["network_tunnel"] = {
            "local_proxy_port": network_local_port,
            "remote_proxy_port": network_remote_port,
            "proxy_url": build_network_proxy_url(network_remote_port, scheme=network_proxy_scheme),
        }
    return payload


def should_use_terminal_launch_script(command: str) -> bool:
    return len(command) > 2000 or bool(AI_SESSION_TOKEN_RE.search(command))


def write_terminal_launch_script(command: str) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="labgpu-ssh-"))
    os.chmod(directory, 0o700)
    script = directory / "open.sh"
    script.write_text(
        "#!/bin/sh\n"
        'script_path="$0"\n'
        'script_dir="$(dirname "$script_path")"\n'
        'rm -f "$script_path"\n'
        'rmdir "$script_dir" 2>/dev/null || true\n'
        f"exec {command}\n",
        encoding="utf-8",
    )
    os.chmod(script, 0o700)
    return script


def cleanup_terminal_launch_script(script: Path | None) -> None:
    if not script:
        return
    try:
        script.unlink(missing_ok=True)
        script.parent.rmdir()
    except OSError:
        return


def build_ssh_terminal_argv(
    alias: str,
    *,
    host: SSHHost | None = None,
    proxy_port: str | int | None = None,
    local_proxy_port: str | int | None = None,
    remote_proxy_port: str | int | None = None,
    agent: str = "none",
    ai_mode: str | None = None,
    provider_name: str | None = None,
    gpu_index: str | int | None = None,
    remote_cwd: str | None = None,
    local_gateway_port: str | int | None = None,
    session_token: str | None = None,
    network_local_proxy_port: str | int | None = None,
    network_remote_proxy_port: str | int | None = None,
    network_proxy_scheme: str = "http",
) -> list[str]:
    if not is_safe_ssh_alias(alias):
        raise ValueError("Unsafe SSH alias.")
    normalized_agent = normalize_terminal_agent(agent)
    if ai_mode in AI_SESSION_MODES and normalized_agent not in AI_PROXY_TUNNEL_AGENTS:
        raise ValueError("Only Claude Code and Codex CLI AI sessions are available in this alpha.")
    local_port, remote_port = normalize_proxy_ports(
        proxy_port=proxy_port,
        local_proxy_port=local_proxy_port,
        remote_proxy_port=remote_proxy_port,
    )
    network_local_port, network_remote_port = normalize_proxy_ports(
        local_proxy_port=network_local_proxy_port,
        remote_proxy_port=network_remote_proxy_port,
    )
    forward_requested = bool((local_port and remote_port) or (network_local_port and network_remote_port))
    ssh_options, ssh_target = isolated_ssh_args(host) if host and forward_requested else ([], alias)
    remote_path_prefixes = ai_path_prefixes_for_host(host)
    claude_command = normalized_remote_command_path(host.claude_command) if host else None
    codex_command = normalized_remote_command_path(host.codex_command) if host else None
    if normalized_agent in AI_PROXY_TUNNEL_AGENTS and ai_mode in AI_SESSION_MODES:
        if not local_port or not remote_port:
            raise ValueError(f"{ai_agent_label(normalized_agent)} AI session requires a CC Switch proxy port and remote gateway port.")
        gateway_port = normalize_proxy_port(local_gateway_port)
        if not gateway_port:
            raise ValueError(f"{ai_agent_label(normalized_agent)} AI session requires a local AI gateway port.")
        return build_ai_ssh_command(
            EnterServerAIRequest(
                server_alias=alias,
                gpu_index=str(gpu_index or ""),
                ai_app=normalized_agent,
                provider_name=str(provider_name or ""),
                ccswitch_proxy_port=local_port,
                local_gateway_port=gateway_port,
                remote_gateway_port=remote_port,
                session_token=str(session_token or ""),
                mode=str(ai_mode or "proxy_tunnel"),
                remote_cwd=remote_cwd,
                ssh_options=tuple(ssh_options),
                ssh_target=ssh_target,
                remote_path_prefixes=remote_path_prefixes,
                claude_command=claude_command,
                codex_command=codex_command,
                network_proxy_local_port=network_local_port,
                network_proxy_remote_port=network_remote_port,
                network_proxy_scheme=network_proxy_scheme,
            )
        ).ssh_args
    remote_command = terminal_remote_command(
        remote_port,
        normalized_agent,
        local_proxy_port=local_port,
        remote_cwd=remote_cwd,
        remote_path_prefixes=remote_path_prefixes,
        claude_command=claude_command,
        codex_command=codex_command,
        network_proxy_port=network_remote_port,
        network_local_proxy_port=network_local_port,
        network_proxy_scheme=network_proxy_scheme,
    )
    argv = ["ssh", *ssh_options]
    if local_port and remote_port:
        argv.extend(["-o", "ExitOnForwardFailure=yes", "-R", f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}"])
    if network_local_port and network_remote_port:
        if "-o" not in argv or "ExitOnForwardFailure=yes" not in argv:
            argv.extend(["-o", "ExitOnForwardFailure=yes"])
        argv.extend(["-R", f"127.0.0.1:{network_remote_port}:127.0.0.1:{network_local_port}"])
    if remote_command:
        argv.extend(["-t", ssh_target, remote_command])
    else:
        argv.append(ssh_target)
    return argv


def isolated_ssh_args(host: SSHHost) -> tuple[list[str], str]:
    """Build ssh argv options that preserve login config but ignore configured forwards."""
    options: list[str] = ["-F", "/dev/null"]

    def add_option(name: str, value: object) -> None:
        text = str(value or "").strip()
        if text and text.lower() != "none":
            options.extend(["-o", f"{name}={text}"])

    add_option("HostName", host.hostname)
    add_option("User", host.user)
    add_option("Port", host.port)
    add_option("ProxyJump", host.proxyjump)
    for identity_file in host.identity_files:
        add_option("IdentityFile", identity_file)
    for source_key, option_name in {
        "proxycommand": "ProxyCommand",
        "identityagent": "IdentityAgent",
        "identitiesonly": "IdentitiesOnly",
        "forwardagent": "ForwardAgent",
        "serveraliveinterval": "ServerAliveInterval",
        "serveralivecountmax": "ServerAliveCountMax",
        "stricthostkeychecking": "StrictHostKeyChecking",
        "userknownhostsfile": "UserKnownHostsFile",
        "hostkeyalias": "HostKeyAlias",
    }.items():
        add_option(option_name, host.options.get(source_key))
    return options, host.alias


def ai_path_prefixes_for_host(host: SSHHost | None) -> tuple[str, ...]:
    extra = list(getattr(host, "ai_extra_paths", []) or []) if host else []
    return normalized_remote_path_prefixes([*DEFAULT_AI_PATH_PREFIXES, *extra])


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


def ai_agent_label(agent: str) -> str:
    return AI_APP_LABELS.get(agent, agent)


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


def terminal_remote_command(
    proxy_port: int | None,
    agent: str,
    *,
    local_proxy_port: int | None = None,
    remote_cwd: str | None = None,
    remote_path_prefixes: tuple[str, ...] | list[str] = DEFAULT_AI_PATH_PREFIXES,
    claude_command: str | None = None,
    codex_command: str | None = None,
    network_proxy_port: int | None = None,
    network_local_proxy_port: int | None = None,
    network_proxy_scheme: str = "http",
) -> str:
    parts: list[str] = []
    cwd = normalized_remote_cwd(remote_cwd)
    path_export = build_path_export(remote_path_prefixes)
    if path_export and agent != "none":
        parts.append(path_export)
    command_path = normalized_remote_command_path(claude_command)
    if command_path is not None:
        parts.append(f"export LABGPU_AI_CLAUDE_COMMAND={shlex.quote(command_path)}")
    codex_path = normalized_remote_command_path(codex_command)
    if codex_path is not None:
        parts.append(f"export LABGPU_AI_CODEX_COMMAND={shlex.quote(codex_path)}")
    if cwd is not None:
        parts.append(f"export LABGPU_REMOTE_CWD={shlex.quote(cwd)}")
        parts.append(f"cd {shlex.quote(cwd)} || exit 1")
    if proxy_port:
        proxy_url = f"http://127.0.0.1:{proxy_port}"
        parts.append(f"export HTTP_PROXY={shlex.quote(proxy_url)} HTTPS_PROXY={shlex.quote(proxy_url)}")
        local_text = f"127.0.0.1:{local_proxy_port or proxy_port}"
        parts.append(f"echo 'LabGPU proxy: remote HTTP_PROXY/HTTPS_PROXY -> local {local_text}'")
    network_proxy_url = build_network_proxy_url(network_proxy_port, scheme=network_proxy_scheme)
    if network_proxy_url:
        parts.append(
            " ".join(
                f"export {name}={shlex.quote(network_proxy_url)}"
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
            )
        )
        local_text = f"127.0.0.1:{network_local_proxy_port or network_proxy_port}"
        parts.append(f"echo 'LabGPU network tunnel: remote proxy {network_proxy_url} -> local {local_text}'")
    if agent != "none":
        parts.append(agent_launcher_command(agent, remote_path_prefixes=remote_path_prefixes, claude_command=command_path, codex_command=codex_path))
    elif proxy_port or network_proxy_port or cwd is not None:
        parts.append('if [ -n "${SHELL:-}" ]; then exec "$SHELL" -l; fi; exec /bin/sh')
    return "; ".join(parts)


def agent_launcher_command(agent: str, *, remote_path_prefixes: tuple[str, ...] | list[str] = DEFAULT_AI_PATH_PREFIXES, claude_command: str | None = None, codex_command: str | None = None) -> str:
    path_export = build_path_export(remote_path_prefixes)
    claude_path = normalized_remote_command_path(claude_command)
    claude_check = "command -v claude >/dev/null 2>&1 || command -v claude-code >/dev/null 2>&1"
    claude_launch = "if command -v claude >/dev/null 2>&1; then claude; else claude-code; fi"
    if claude_path is not None:
        quoted = shlex.quote(claude_path)
        claude_check = f"[ -x {quoted} ] || {claude_check}"
        claude_launch = f"if [ -x {quoted} ]; then {quoted}; elif command -v claude >/dev/null 2>&1; then claude; else claude-code; fi"
    codex_path = normalized_remote_command_path(codex_command)
    codex_check = 'command -v codex >/dev/null 2>&1'
    codex_launch = "codex"
    if codex_path is not None:
        quoted = shlex.quote(codex_path)
        codex_check = f"[ -x {quoted} ] || {codex_check}"
        codex_launch = f"if [ -x {quoted} ]; then {quoted}; else codex; fi"
    launchers = {
        "codex": (codex_check, codex_launch, "Codex CLI was not found."),
        "claude": (
            claude_check,
            claude_launch,
            "claude not found in LabGPU launch PATH.",
        ),
        "gemini": ('command -v gemini >/dev/null 2>&1', "gemini", "Gemini CLI was not found."),
        "openclaw": ('command -v openclaw >/dev/null 2>&1', "openclaw agent", "OpenClaw CLI was not found."),
    }
    if agent not in launchers:
        raise ValueError("Unsupported terminal launcher.")
    check, command, missing = launchers[agent]
    prefix = f"{path_export}; " if path_export else ""
    script = f'if {check}; then {command}; else echo "{missing}"; fi; exec ${{SHELL:-/bin/sh}} -il'
    return f"exec ${{SHELL:-/bin/sh}} -ic {shlex.quote(prefix + script)}"


def is_local_tcp_port_open(port: int) -> bool | None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except PermissionError:
        return None
    except OSError:
        return False


def close_gateway_session(gateway: AIGatewaySession) -> None:
    try:
        gateway.close()
    finally:
        if gateway in AI_GATEWAY_SESSIONS:
            AI_GATEWAY_SESSIONS.remove(gateway)


def redact_ai_session_tokens(command: str) -> str:
    return AI_SESSION_TOKEN_RE.sub("labgpu-session-<redacted>", command)


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
