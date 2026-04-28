from __future__ import annotations

from labgpu.remote.dashboard import serve


def run(args) -> int:
    serve(
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        open_browser=not args.no_open,
        allow_actions=False,
        fake_lab=True,
    )
    return 0
