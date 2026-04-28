from __future__ import annotations

import json

from labgpu.remote.dashboard import collect_servers, serve, split_hosts
from labgpu.remote.ssh_config import parse_ssh_config, resolve_ssh_host, select_hosts


def run_dashboard(args) -> int:
    names = split_hosts(args.hosts)
    if args.json:
        data = collect_servers(
            ssh_config=args.config,
            names=names,
            pattern=args.pattern,
            timeout=args.timeout,
        )
        print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    serve(
        host=args.bind,
        port=args.port,
        ssh_config=args.config,
        names=names,
        pattern=args.pattern,
        timeout=args.timeout,
        open_browser=getattr(args, "open", False),
        allow_actions=getattr(args, "allow_actions", False),
    )
    return 0


def run_list(args) -> int:
    names = split_hosts(getattr(args, "hosts", None))
    hosts = select_hosts(parse_ssh_config(args.config), names=names, pattern=args.pattern)
    hosts = [resolve_ssh_host(host) for host in hosts]
    if args.json:
        print(json.dumps([host_summary(host) for host in hosts], indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if not hosts:
        print("No SSH hosts found.")
        return 0
    print(f"{'Alias':<24} {'HostName':<28} {'User':<16} {'Port':<6} ProxyJump")
    for host in hosts:
        print(
            f"{host.alias:<24} {(host.hostname or '-'):<28} {(host.user or '-'):<16} {(host.port or '22'):<6} {host.proxyjump or '-'}"
        )
    return 0


def run_probe(args) -> int:
    if args.all:
        names = split_hosts(getattr(args, "hosts", None))
    elif args.alias:
        names = [args.alias]
    else:
        raise SystemExit("usage: labgpu servers probe ALIAS or labgpu servers probe --all")
    data = collect_servers(
        ssh_config=args.config,
        names=names,
        pattern=args.pattern if args.all else None,
        timeout=args.timeout,
    )
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    for host in data.get("hosts", []):
        gpus = host.get("gpus") or []
        busy = sum(1 for gpu in gpus if gpu.get("processes"))
        status = "online" if host.get("online") else "offline"
        detail = host.get("error") or f"{len(gpus)} GPUs, {len(gpus) - busy} free, {busy} busy"
        print(f"{host.get('alias')}: {status} - {detail}")
    return 0


def host_summary(host) -> dict[str, object]:
    return {
        "alias": host.alias,
        "hostname": host.hostname,
        "user": host.user,
        "port": host.port,
        "proxyjump": host.proxyjump,
    }
