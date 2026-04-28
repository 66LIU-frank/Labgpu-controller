from __future__ import annotations

import sys
from typing import Iterable

from labgpu.core.config import ServerEntry, load_config, write_config
from labgpu.remote.probe import DEFAULT_DISK_PATHS, probe_host
from labgpu.remote.ssh_config import SSHHost, parse_ssh_config, resolve_ssh_host, select_hosts


MODEL_TAGS = ("A100", "H100", "H800", "4090", "3090", "A6000", "L40", "V100")


def run(args) -> int:
    ssh_hosts = parse_ssh_config(args.config)
    selected = choose_hosts(
        ssh_hosts,
        names=split_csv(getattr(args, "hosts", None)),
        pattern=getattr(args, "pattern", None),
    )
    if not selected:
        raise RuntimeError("no SSH hosts selected; add hosts to ~/.ssh/config or pass --hosts")

    shared_account = bool(getattr(args, "shared_account", False))
    if not shared_account and sys.stdin.isatty():
        shared_account = prompt_yes_no("Are these shared Linux accounts used by multiple people?", default=False)

    base_tags = split_csv(getattr(args, "tags", None)) or ["training"]
    lab_config = load_config(getattr(args, "labgpu_config", None))
    if not getattr(args, "keep_existing", False):
        for entry in lab_config.servers.values():
            entry.enabled = False

    saved: list[ServerEntry] = []
    for host in selected:
        tags = list(base_tags)
        resolved = resolve_ssh_host(host)
        if not getattr(args, "no_probe", False):
            print(f"Probing {host.alias}...")
            payload = probe_host(resolved, timeout=getattr(args, "timeout", 8))
            tags = merge_tags(tags, detect_model_tags(payload))
            if payload.get("online"):
                print(f"  online: {len(payload.get('gpus') or [])} GPU(s)")
            else:
                print(f"  saved, but probe failed: {payload.get('error') or 'offline'}")

        existing = lab_config.servers.get(host.alias)
        entry = existing or ServerEntry(name=host.alias, alias=host.alias)
        entry.enabled = True
        entry.alias = host.alias
        entry.tags = merge_tags(entry.tags, tags)
        if not entry.disk_paths:
            entry.disk_paths = list(DEFAULT_DISK_PATHS)
        entry.shared_account = shared_account
        entry.allow_stop_own_process = not shared_account
        lab_config.servers[entry.name] = entry
        saved.append(entry)

    path = write_config(lab_config, getattr(args, "labgpu_config", None))
    print(f"Saved {len(saved)} server(s) to {path}:")
    for entry in saved:
        tags = ",".join(entry.tags) if entry.tags else "-"
        safety = "shared account; stop disabled" if entry.shared_account else "personal account; stop own process enabled"
        print(f"- {entry.alias}  tags={tags}  {safety}")
    print("")
    print("Next:")
    print("  labgpu ui")
    print("  labgpu pick --min-vram 24G --prefer A100")
    return 0


def choose_hosts(hosts: list[SSHHost], *, names: list[str] | None = None, pattern: str | None = None) -> list[SSHHost]:
    if names or pattern:
        return select_hosts(hosts, names=names, pattern=pattern)
    if not hosts:
        return []
    if not sys.stdin.isatty():
        return hosts
    print("SSH hosts from ~/.ssh/config:")
    for index, host in enumerate(hosts, start=1):
        detail = host.hostname or host.alias
        print(f"  {index}. {host.alias} ({detail})")
    raw = input("Select GPU servers by number or alias, comma-separated [all]: ").strip()
    if not raw:
        return hosts
    return select_prompted_hosts(hosts, raw)


def select_prompted_hosts(hosts: list[SSHHost], raw: str) -> list[SSHHost]:
    selected: list[SSHHost] = []
    by_alias = {host.alias: host for host in hosts}
    for item in split_csv(raw):
        host: SSHHost | None = None
        if item.isdigit():
            index = int(item) - 1
            if 0 <= index < len(hosts):
                host = hosts[index]
        else:
            host = by_alias.get(item) or SSHHost(alias=item)
        if host and host.alias not in {seen.alias for seen in selected}:
            selected.append(host)
    return selected


def detect_model_tags(payload: dict[str, object]) -> list[str]:
    tags: list[str] = []
    for gpu in payload.get("gpus") or []:
        if not isinstance(gpu, dict):
            continue
        name = str(gpu.get("name") or "")
        for tag in MODEL_TAGS:
            if tag.lower() in name.lower():
                tags.append(tag)
    return tags


def merge_tags(*groups: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for tag in group:
            text = str(tag).strip()
            if not text:
                continue
            key = text.lower()
            if key not in seen:
                seen.add(key)
                result.append(text)
    return result


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def prompt_yes_no(question: str, *, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


__all__ = [
    "choose_hosts",
    "detect_model_tags",
    "merge_tags",
    "run",
    "select_prompted_hosts",
    "split_csv",
]
