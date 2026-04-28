from __future__ import annotations

from labgpu.remote.dashboard import serve, split_hosts


def run(args) -> int:
    serve(
        host=args.host,
        port=args.port,
        ssh_config=args.config,
        names=split_hosts(args.hosts),
        pattern=args.pattern,
        timeout=args.timeout,
        open_browser=not args.no_open,
        allow_actions=args.allow_actions,
        fake_lab=args.fake_lab,
    )
    return 0
