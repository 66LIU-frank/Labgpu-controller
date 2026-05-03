from __future__ import annotations

import sys

from labgpu.cli.main import main as cli_main


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        args = ["desktop"]
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
