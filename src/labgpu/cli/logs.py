from __future__ import annotations

import sys
import time
from pathlib import Path

from labgpu.core.store import RunStore
from labgpu.cli.resolve import resolve_run


def run(args) -> int:
    store = RunStore()
    meta = resolve_run(store, args.ref)
    if not meta.log_path:
        raise RuntimeError("no log attached to this run; for adopted runs use: labgpu adopt <pid> --name <name> --log path/to/log")
    path = Path(meta.log_path)
    if not path.exists():
        raise RuntimeError(f"log file does not exist: {path}")
    if args.follow:
        _follow(path)
        return 0
    print(_tail(path, args.tail), end="")
    return 0


def _tail(path: Path, lines: int) -> str:
    data = tail_bytes(path, lines=lines)
    return data.decode(errors="replace") + ("\n" if data else "")


def _follow(path: Path) -> None:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        try:
            while True:
                chunk = handle.read()
                if chunk:
                    sys.stdout.write(chunk.decode(errors="replace"))
                    sys.stdout.flush()
                time.sleep(1)
        except KeyboardInterrupt:
            return


def tail_bytes(path: Path, *, lines: int, max_bytes: int = 1_000_000) -> bytes:
    if lines <= 0:
        return b""
    chunk_size = 8192
    data = b""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        while position > 0 and data.count(b"\n") <= lines and len(data) < max_bytes:
            read_size = min(chunk_size, position, max_bytes - len(data))
            if read_size <= 0:
                break
            position -= read_size
            handle.seek(position)
            data = handle.read(read_size) + data
    return b"\n".join(data.splitlines()[-lines:])
