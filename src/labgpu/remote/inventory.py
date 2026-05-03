from __future__ import annotations

from pathlib import Path

from labgpu.core.config import LabGPUConfig, ServerEntry, load_config, write_config
from labgpu.remote.ssh_config import SSHHost, parse_ssh_config, select_hosts


def load_inventory(
    *,
    ssh_config: str | Path | None = None,
    names: list[str] | None = None,
    pattern: str | None = None,
    config_path: str | Path | None = None,
) -> list[SSHHost]:
    lab_config = load_config(config_path)
    ssh_hosts = parse_ssh_config(ssh_config)

    if names or pattern:
        hosts = select_hosts(ssh_hosts, names=names, pattern=pattern)
        return [apply_server_entry(host, lab_config.servers.get(host.alias)) for host in hosts]

    enabled = [entry for entry in lab_config.servers.values() if entry.enabled]
    if enabled:
        by_alias = {host.alias: host for host in ssh_hosts}
        return [apply_server_entry(by_alias.get(entry.alias, SSHHost(alias=entry.alias)), entry) for entry in enabled]

    return ssh_hosts


def import_ssh_hosts(
    *,
    ssh_config: str | Path | None = None,
    names: list[str] | None = None,
    pattern: str | None = None,
    tags: list[str] | None = None,
    group: str | None = None,
    config_path: str | Path | None = None,
) -> tuple[LabGPUConfig, list[ServerEntry]]:
    lab_config = load_config(config_path)
    hosts = select_hosts(parse_ssh_config(ssh_config), names=names, pattern=pattern)
    imported: list[ServerEntry] = []
    for host in hosts:
        existing = lab_config.servers.get(host.alias)
        entry = existing or ServerEntry(name=host.alias, alias=host.alias)
        entry.enabled = True
        if tags:
            entry.tags = sorted(set(entry.tags).union(tags))
        if group is not None:
            entry.group = group.strip()
            if entry.group and entry.group not in lab_config.groups:
                lab_config.groups.append(entry.group)
        if not entry.disk_paths:
            entry.disk_paths = ["/", "/home", "/data", "/scratch", "/mnt", "/nvme"]
        lab_config.servers[entry.name] = entry
        imported.append(entry)
    write_config(lab_config, config_path)
    return lab_config, imported


def apply_server_entry(host: SSHHost, entry: ServerEntry | None) -> SSHHost:
    if not entry:
        return host
    host.group = entry.group
    host.tags = list(entry.tags)
    host.disk_paths = list(entry.disk_paths)
    host.shared_account = entry.shared_account
    host.allow_stop_own_process = entry.allow_stop_own_process
    host.ai_extra_paths = list(entry.ai_extra_paths)
    host.claude_command = entry.claude_command
    host.codex_command = entry.codex_command
    return host
