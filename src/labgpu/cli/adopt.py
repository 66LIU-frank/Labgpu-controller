from __future__ import annotations

import getpass
import json
import platform
from pathlib import Path

from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore
from labgpu.process.inspector import inspect_process, pid_exists
from labgpu.runner.base import make_run_id
from labgpu.utils.git import git_metadata
from labgpu.utils.time import now_utc


def run(args) -> int:
    if not pid_exists(args.pid):
        raise RuntimeError(f"pid {args.pid} is not running")
    info = inspect_process(args.pid)
    cwd = Path(info.get("cwd") or Path.cwd()).resolve()
    if not cwd.exists():
        cwd = Path.cwd().resolve()
    store = RunStore()
    run_id = make_run_id(args.name)
    run_dir = store.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    log_path = Path(args.log).expanduser().resolve() if args.log else run_dir / "adopted.log"
    if not args.log:
        log_path.write_text("[labgpu] adopted run has no original stdout/stderr log\n", encoding="utf-8")
    git = git_metadata(cwd)
    meta = RunMeta(
        run_id=run_id,
        name=args.name,
        user=str(info.get("user") or getpass.getuser()),
        host=platform.node() or "localhost",
        status="running",
        created_at=now_utc(),
        started_at=now_utc(),
        command=str(info.get("command") or f"pid {args.pid}"),
        cwd=str(cwd),
        requested_gpu_indices=[item.strip() for item in args.gpu.split(",")] if args.gpu else [],
        cuda_visible_devices=args.gpu,
        pid=args.pid,
        log_path=str(log_path),
        launch_mode="adopted",
        project=args.project,
        tags=args.tag,
        note=args.note,
        **git,
    )
    store.write(meta)
    (run_dir / "adopted.json").write_text(json.dumps(info, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Adopted: {args.pid} -> {run_id}")
    return 0
