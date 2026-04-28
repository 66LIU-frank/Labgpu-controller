from __future__ import annotations

import glob
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SSHHost:
    alias: str
    hostname: str | None = None
    user: str | None = None
    port: str | None = None
    proxyjump: str | None = None
    identity_files: list[str] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)


def default_ssh_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def parse_ssh_config(path: str | Path | None = None, _seen: set[Path] | None = None) -> list[SSHHost]:
    path = Path(path).expanduser() if path else default_ssh_config_path()
    path = path.resolve()
    _seen = _seen or set()
    if path in _seen:
        return []
    _seen.add(path)
    if not path.exists():
        return []
    hosts: list[SSHHost] = []
    current: list[SSHHost] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        key = parts[0]
        value = parts[1].strip() if len(parts) > 1 else ""
        key_lower = key.lower()
        if key_lower == "include":
            for include in value.split():
                for include_path in _include_paths(include, path.parent):
                    hosts.extend(parse_ssh_config(include_path, _seen))
            continue
        if key_lower == "host":
            current = []
            for alias in value.split():
                if _is_concrete_alias(alias):
                    host = SSHHost(alias=alias)
                    hosts.append(host)
                    current.append(host)
            continue
        if not current or not value:
            continue
        for host in current:
            host.options[key_lower] = value
            if key_lower == "hostname":
                host.hostname = value
            elif key_lower == "user":
                host.user = value
            elif key_lower == "port":
                host.port = value
            elif key_lower == "proxyjump":
                host.proxyjump = value
            elif key_lower == "identityfile":
                host.identity_files.append(value)
    return hosts


def resolve_ssh_host(host: SSHHost, *, timeout: int = 3) -> SSHHost:
    """Resolve effective OpenSSH options with `ssh -G` when available.

    This does not connect to the remote host; it asks the local ssh client to
    expand Include files, Host * defaults, ProxyJump, IdentityFile, and aliases.
    """
    try:
        result = subprocess.run(
            ["ssh", "-G", host.alias],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return host
    if result.returncode != 0:
        return host
    parsed = parse_ssh_g(result.stdout)
    options = {**host.options, **{key: value for key, value in parsed.items() if isinstance(value, str)}}
    return SSHHost(
        alias=host.alias,
        hostname=_first_str(parsed.get("hostname")) or host.hostname,
        user=_first_str(parsed.get("user")) or host.user,
        port=_first_str(parsed.get("port")) or host.port,
        proxyjump=_first_str(parsed.get("proxyjump")) or host.proxyjump,
        identity_files=list(parsed.get("identityfile") or host.identity_files),
        options=options,
    )


def parse_ssh_g(output: str) -> dict[str, str | list[str]]:
    parsed: dict[str, str | list[str]] = {}
    identity_files: list[str] = []
    for raw in output.splitlines():
        parts = raw.strip().split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1].strip()
        if key == "identityfile":
            identity_files.append(value)
        else:
            parsed[key] = value
    if identity_files:
        parsed["identityfile"] = identity_files
    return parsed


def select_hosts(
    hosts: list[SSHHost],
    *,
    names: list[str] | None = None,
    pattern: str | None = None,
) -> list[SSHHost]:
    selected = hosts
    if names:
        by_alias = {host.alias: host for host in selected}
        selected = [by_alias.get(name) or SSHHost(alias=name) for name in names]
    if pattern:
        selected = [host for host in selected if pattern.lower() in host.alias.lower()]
    return selected


def _is_concrete_alias(alias: str) -> bool:
    return not any(ch in alias for ch in "*?!")


def _include_paths(pattern: str, base: Path) -> list[Path]:
    expanded = Path(pattern).expanduser()
    if not expanded.is_absolute():
        expanded = base / expanded
    matches = sorted(glob.glob(str(expanded)))
    if matches:
        return [Path(match) for match in matches]
    return [expanded]


def _first_str(value: str | list[str] | None) -> str | None:
    if isinstance(value, list):
        return value[0] if value else None
    return value
