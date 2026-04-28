from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SSHHost:
    alias: str
    hostname: str | None = None
    user: str | None = None
    port: str | None = None
    options: dict[str, str] = field(default_factory=dict)


def default_ssh_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def parse_ssh_config(path: str | Path | None = None) -> list[SSHHost]:
    path = Path(path).expanduser() if path else default_ssh_config_path()
    if not path.exists():
        return []
    hosts: list[SSHHost] = []
    current: list[SSHHost] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        key_lower = key.lower()
        value = value.strip()
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
    return hosts


def select_hosts(
    hosts: list[SSHHost],
    *,
    names: list[str] | None = None,
    pattern: str | None = None,
) -> list[SSHHost]:
    selected = hosts
    if names:
        wanted = set(names)
        selected = [host for host in selected if host.alias in wanted]
    if pattern:
        selected = [host for host in selected if pattern.lower() in host.alias.lower()]
    return selected


def _is_concrete_alias(alias: str) -> bool:
    return not any(ch in alias for ch in "*?!")
