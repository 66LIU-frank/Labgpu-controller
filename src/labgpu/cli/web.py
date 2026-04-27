from __future__ import annotations

from labgpu.web.app import serve


def run(args) -> int:
    serve(args.host, args.port, fake=args.fake)
    return 0
