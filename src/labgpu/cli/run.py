from __future__ import annotations

from labgpu.gpu.select import pick_local_gpu
from labgpu.remote.ranking import format_memory, parse_vram_to_mb
from labgpu.runner.base import create_and_start_run
from labgpu.utils.shell import strip_command_separator


def run(args) -> int:
    command = strip_command_separator(args.command)
    gpu = resolve_gpu(args.gpu, min_vram=getattr(args, "min_vram", None), prefer=getattr(args, "prefer", None))
    meta = create_and_start_run(
        name=args.name,
        command=command,
        gpu=gpu,
        project=args.project,
        tags=args.tag,
        note=args.note,
        configs=args.config,
    )
    print(f"Started: {meta.run_id}")
    print(f"Log: {meta.log_path}")
    print(f"tmux: {meta.tmux_session}")
    return 0


def resolve_gpu(gpu: str | None, *, min_vram: str | None = None, prefer: str | None = None) -> str | None:
    if gpu != "auto":
        return gpu
    min_vram_mb = parse_vram_to_mb(min_vram or "") if min_vram else None
    if min_vram and min_vram_mb is None:
        raise RuntimeError(f"invalid --min-vram value: {min_vram}")
    selected = pick_local_gpu(min_vram_mb=min_vram_mb, prefer=prefer)
    index = str(selected.get("index"))
    free = format_memory(selected.get("memory_free_mb"))
    name = selected.get("name") or "GPU"
    print(f"Auto GPU: selected GPU {index} ({name}, {free} free)")
    return index
