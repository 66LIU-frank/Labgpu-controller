from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


def shlex_join(argv: list[str]) -> str:
    return shlex.join(argv)


def run_text(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def strip_command_separator(argv: list[str]) -> list[str]:
    if argv and argv[0] == "--":
        return argv[1:]
    return argv


def short_command(command: str, width: int = 72) -> str:
    command = " ".join(command.split())
    if len(command) <= width:
        return command
    return command[: max(0, width - 1)] + "..."
