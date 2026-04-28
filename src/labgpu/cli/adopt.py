from __future__ import annotations

import getpass
import json
import os
import platform
import sys
from pathlib import Path

from labgpu.core.events import append_event
from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore
from labgpu.gpu.select import detect_pid_gpus
from labgpu.process.inspector import inspect_process, pid_exists
from labgpu.runner.base import make_run_id
from labgpu.utils.git import git_metadata
from labgpu.utils.time import now_utc


def run(args) -> int:
    if not pid_exists(args.pid):
        raise RuntimeError(f"pid {args.pid} is not running")
    info = inspect_process(args.pid)
    ensure_owner_allowed(info, allow_other_owner=getattr(args, "allow_other_owner", False))
    gpu = resolve_adopt_gpu(args.pid, args.gpu)
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
    env_json_path = run_dir / "env.json"
    git_json_path = run_dir / "git.json"
    env_json_path.write_text(
        json.dumps(
            {
                "python_version": sys.version.split()[0],
                "working_directory": str(cwd),
                "CUDA_VISIBLE_DEVICES": gpu,
                "CUDA_DEVICE_ORDER": os.environ.get("CUDA_DEVICE_ORDER"),
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    git_json_path.write_text(json.dumps(git, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
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
        requested_gpu_indices=[item.strip() for item in gpu.split(",")] if gpu else [],
        cuda_visible_devices=gpu,
        pid=args.pid,
        log_path=str(log_path),
        git_json_path=str(git_json_path),
        env_json_path=str(env_json_path),
        launch_mode="adopted",
        project=args.project,
        tags=args.tag,
        note=args.note,
        **git,
    )
    store.write(meta)
    append_event(run_dir, "adopted", pid=args.pid, gpu=gpu, log_path=str(log_path), process_start_time=info.get("create_time"))
    adopted_payload = {
        **info,
        "gpu": gpu,
        "log_path": str(log_path),
        "process_start_time": info.get("create_time"),
    }
    (run_dir / "adopted.json").write_text(json.dumps(adopted_payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Adopted: {args.pid} -> {run_id}")
    return 0


def ensure_owner_allowed(info: dict[str, object], *, allow_other_owner: bool = False) -> None:
    owner = str(info.get("user") or info.get("username") or "")
    current = getpass.getuser()
    if owner and owner != current and not allow_other_owner:
        raise RuntimeError(
            f"pid {info.get('pid')} is owned by {owner}, not {current}. "
            "LabGPU is personal-first; use --allow-other-owner only to create a local note."
        )


def resolve_adopt_gpu(pid: int, gpu: str | None) -> str | None:
    if gpu:
        return gpu
    detected = detect_pid_gpus(pid)
    if not detected:
        print("GPU: not detected; pass --gpu if you know the CUDA device.")
        return None
    value = ",".join(detected)
    print(f"GPU: detected PID {pid} on GPU {value}")
    return value
