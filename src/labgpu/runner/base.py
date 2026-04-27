from __future__ import annotations

import getpass
import json
import os
import platform
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

from labgpu.core.events import append_event
from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore
from labgpu.runner.tmux import start_tmux
from labgpu.utils.git import git_metadata, write_git_patch
from labgpu.utils.shell import shlex_join
from labgpu.utils.time import now_utc


def create_and_start_run(
    *,
    name: str,
    command: list[str],
    gpu: str | None,
    project: str | None = None,
    tags: list[str] | None = None,
    note: str | None = None,
    configs: list[str] | None = None,
) -> RunMeta:
    if not command:
        raise ValueError("missing command after --")
    cwd = Path.cwd().resolve()
    run_id = make_run_id(name)
    store = RunStore()
    run_dir = store.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    log_path = run_dir / "stdout.log"
    config_snapshot_dir = snapshot_configs(configs or [], cwd, run_dir)
    git = git_metadata(cwd)
    patch_path = write_git_patch(cwd, run_dir / "git.patch") if git["git_dirty"] else None
    env = {"CUDA_DEVICE_ORDER": "PCI_BUS_ID"}
    if gpu:
        env["CUDA_VISIBLE_DEVICES"] = gpu
    env_snapshot = environment_snapshot(env)
    env_json_path = run_dir / "env.json"
    git_json_path = run_dir / "git.json"
    env_json_path.write_text(json.dumps(env_snapshot, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    git_payload = {**git, "git_status": "available" if git.get("git_commit") else "not_available"}
    git_json_path.write_text(json.dumps(git_payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmux_session = f"labgpu_{run_id}".replace(".", "_")
    meta = RunMeta(
        run_id=run_id,
        name=name,
        user=getpass.getuser(),
        host=platform.node() or "localhost",
        status="created",
        created_at=now_utc(),
        command=shlex_join(command),
        command_argv=command,
        cwd=str(cwd),
        env=env,
        requested_gpu_indices=[item.strip() for item in gpu.split(",")] if gpu else [],
        cuda_visible_devices=gpu,
        tmux_session=tmux_session,
        log_path=str(log_path),
        git_patch_path=patch_path,
        git_json_path=str(git_json_path),
        config_paths=configs or [],
        config_snapshot_dir=str(config_snapshot_dir) if config_snapshot_dir else None,
        python_version=env_snapshot["python_version"],
        conda_env=env_snapshot["conda_env"],
        virtual_env=env_snapshot["virtual_env"],
        env_json_path=str(env_json_path),
        launch_mode="labgpu",
        project=project,
        tags=tags or [],
        note=note,
        **git,
    )
    store.write(meta)
    append_event(run_dir, "created", status=meta.status)
    write_command_script(run_dir, meta)
    try:
        start_tmux(run_id, tmux_session, cwd, env)
    except Exception as exc:
        store.update(
            run_id,
            status="failed",
            ended_at=now_utc(),
            failure_reason=f"tmux failed: {exc}",
        )
        raise
    meta = store.update(run_id, status="running")
    return meta


def make_run_id(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() or ch in "._-" else "-" for ch in name).strip("-._")
    slug = slug or "run"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{slug}-{stamp}-{uuid.uuid4().hex[:6]}"


def environment_snapshot(run_env: dict[str, str]) -> dict[str, object]:
    merged = {**os.environ, **run_env}
    return {
        "python_version": sys.version.split()[0],
        "conda_env": os.environ.get("CONDA_DEFAULT_ENV"),
        "virtual_env": os.environ.get("VIRTUAL_ENV"),
        "working_directory": str(Path.cwd().resolve()),
        "PATH": os.environ.get("PATH", ""),
        "CUDA_VISIBLE_DEVICES": merged.get("CUDA_VISIBLE_DEVICES"),
        "CUDA_DEVICE_ORDER": merged.get("CUDA_DEVICE_ORDER"),
    }


def snapshot_configs(files: list[str], cwd: Path, run_dir: Path) -> Path | None:
    if not files:
        return None
    target = run_dir / "config"
    copied = False
    target.mkdir(parents=True, exist_ok=True)
    for raw in files:
        source = Path(raw)
        if not source.is_absolute():
            source = cwd / source
        if not source.exists() or not source.is_file():
            continue
        destination = target / raw.replace("/", "__").replace("\\", "__")
        shutil.copy2(source, destination)
        copied = True
    return target if copied else None


def write_command_script(run_dir: Path, meta: RunMeta) -> None:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "export CUDA_DEVICE_ORDER=PCI_BUS_ID",
    ]
    if meta.cuda_visible_devices:
        lines.append(f"export CUDA_VISIBLE_DEVICES={json.dumps(meta.cuda_visible_devices)}")
    lines.append(f"cd {json.dumps(meta.cwd)}")
    lines.append(meta.command)
    path = run_dir / "command.sh"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
