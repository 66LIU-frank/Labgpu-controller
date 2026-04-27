from __future__ import annotations

from labgpu.runner.base import create_and_start_run
from labgpu.utils.shell import strip_command_separator


def run(args) -> int:
    command = strip_command_separator(args.command)
    meta = create_and_start_run(
        name=args.name,
        command=command,
        gpu=args.gpu,
        project=args.project,
        tags=args.tag,
        note=args.note,
        configs=args.config,
    )
    print(f"Started: {meta.run_id}")
    print(f"Log: {meta.log_path}")
    print(f"tmux: {meta.tmux_session}")
    return 0
