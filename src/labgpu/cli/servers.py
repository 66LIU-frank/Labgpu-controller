from __future__ import annotations

import json

from labgpu.remote.dashboard import collect_servers, serve, split_hosts


def run(args) -> int:
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
    )
    return 0
