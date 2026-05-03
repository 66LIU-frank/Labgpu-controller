from __future__ import annotations

import glob
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
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
    group: str = ""
    tags: list[str] = field(default_factory=list)
    disk_paths: list[str] = field(default_factory=list)
    shared_account: bool = False
    allow_stop_own_process: bool = True
    ai_extra_paths: list[str] = field(default_factory=list)
    claude_command: str = ""
    codex_command: str = ""


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
        group=host.group,
        tags=list(host.tags),
        disk_paths=list(host.disk_paths),
        shared_account=host.shared_account,
        allow_stop_own_process=host.allow_stop_own_process,
        ai_extra_paths=list(host.ai_extra_paths),
        claude_command=host.claude_command,
        codex_command=host.codex_command,
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


def quote_words(values: list[str]) -> str:
    return " ".join(shlex.quote(value) for value in values)


def append_ssh_host(
    *,
    alias: str,
    hostname: str,
    user: str | None = None,
    port: str | None = None,
    proxyjump: str | None = None,
    identity_file: str | None = None,
    path: str | Path | None = None,
) -> tuple[Path, Path | None]:
    """Append a concrete Host block to an OpenSSH config file.

    Existing files are backed up before appending. Existing aliases are not
    overwritten because SSH config merging can be surprising and hard to undo.
    """
    alias = alias.strip()
    hostname = hostname.strip()
    if not _valid_host_token(alias):
        raise ValueError("alias must be a single SSH Host token without spaces, *, ?, or !")
    if not hostname:
        raise ValueError("hostname is required")
    target = Path(path).expanduser() if path else default_ssh_config_path()
    existing = {host.alias for host in parse_ssh_config(target)} if target.exists() else set()
    if alias in existing:
        raise ValueError(f"SSH alias already exists: {alias}")
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    backup: Path | None = None
    if target.exists():
        backup = target.with_name(f"{target.name}.labgpu-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(target.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    block = render_ssh_host_block(
        alias=alias,
        hostname=hostname,
        user=user,
        port=port,
        proxyjump=proxyjump,
        identity_file=identity_file,
    )
    prefix = "\n" if target.exists() and target.read_text(encoding="utf-8", errors="replace").strip() else ""
    with target.open("a", encoding="utf-8") as handle:
        handle.write(prefix + block)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target, backup


def render_ssh_host_block(
    *,
    alias: str,
    hostname: str,
    user: str | None = None,
    port: str | None = None,
    proxyjump: str | None = None,
    identity_file: str | None = None,
) -> str:
    lines = [
        "# Added by LabGPU",
        f"Host {alias.strip()}",
        f"  HostName {hostname.strip()}",
    ]
    if user and user.strip():
        lines.append(f"  User {user.strip()}")
    if port and port.strip():
        lines.append(f"  Port {port.strip()}")
    if proxyjump and proxyjump.strip():
        lines.append(f"  ProxyJump {proxyjump.strip()}")
    if identity_file and identity_file.strip():
        lines.append(f"  IdentityFile {identity_file.strip()}")
    return "\n".join(lines) + "\n"


def _valid_host_token(value: str) -> bool:
    return bool(value) and not any(ch.isspace() or ch in "*?!" for ch in value)
