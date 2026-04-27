from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from labgpu.cli.resolve import resolve_run
from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore


def run(args) -> int:
    store = RunStore()
    meta = resolve_run(store, args.ref)
    payload = build_context(
        store,
        meta,
        tail=args.tail,
        max_bytes=args.max_bytes,
        include_env=args.include_env,
        redact=args.redact,
    )
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(render_markdown(payload))
    return 0


def build_context(
    store: RunStore,
    meta: RunMeta,
    *,
    tail: int = 200,
    max_bytes: int = 200_000,
    include_env: bool = False,
    redact: bool = True,
) -> dict[str, Any]:
    run_dir = store.run_dir(meta.run_id)
    env = read_json(Path(meta.env_json_path)) if meta.env_json_path else read_json(run_dir / "env.json")
    safe_env, redacted_fields = prepare_env(env or {}, include_env=include_env, redact=redact)
    return {
        "run": safe_run_meta(meta),
        "diagnosis": read_json(run_dir / "diagnosis.json"),
        "git": read_json(Path(meta.git_json_path)) if meta.git_json_path else read_json(run_dir / "git.json"),
        "env": safe_env,
        "redacted_fields": redacted_fields,
        "config": config_context(meta, run_dir),
        "log_tail": tail_text(Path(meta.log_path), tail, max_bytes=max_bytes) if meta.log_path else "",
    }


def safe_run_meta(meta: RunMeta) -> dict[str, Any]:
    return {
        "run_id": meta.run_id,
        "name": meta.name,
        "status": meta.status,
        "launch_mode": meta.launch_mode,
        "user": meta.user,
        "host": meta.host,
        "pid": meta.pid,
        "tmux_session": meta.tmux_session,
        "gpu": meta.cuda_visible_devices or ",".join(meta.requested_gpu_indices),
        "created_at": meta.created_at,
        "started_at": meta.started_at,
        "ended_at": meta.ended_at,
        "duration_seconds": meta.duration_seconds,
        "exit_code": meta.exit_code,
        "command": meta.command,
        "cwd": meta.cwd,
        "log_path": meta.log_path,
        "failure_reason": meta.failure_reason,
        "failure_evidence": meta.failure_evidence,
    }


def config_context(meta: RunMeta, run_dir: Path) -> dict[str, Any]:
    snapshot_dir = Path(meta.config_snapshot_dir) if meta.config_snapshot_dir else run_dir / "config"
    files = []
    if snapshot_dir.exists():
        files = [str(path) for path in sorted(snapshot_dir.iterdir()) if path.is_file()]
    return {
        "original_paths": meta.config_paths,
        "snapshot_dir": str(snapshot_dir) if snapshot_dir.exists() else None,
        "snapshot_files": files,
    }


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"error": f"invalid json: {path}"}


def tail_text(path: Path, lines: int, *, max_bytes: int = 200_000) -> str:
    if not path.exists():
        return ""
    from labgpu.cli.logs import tail_bytes

    return tail_bytes(path, lines=lines, max_bytes=max_bytes).decode(errors="replace")


SENSITIVE_ENV_PARTS = (
    "TOKEN",
    "KEY",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "OPENAI_API_KEY",
    "WANDB_API_KEY",
    "HF_TOKEN",
    "GITHUB_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "SSH_AUTH_SOCK",
)


SAFE_ENV_KEYS = (
    "python_version",
    "conda_env",
    "virtual_env",
    "working_directory",
    "CUDA_VISIBLE_DEVICES",
    "CUDA_DEVICE_ORDER",
)


def prepare_env(env: dict[str, Any], *, include_env: bool, redact: bool) -> tuple[dict[str, Any], list[str]]:
    source = dict(env) if include_env else {key: env.get(key) for key in SAFE_ENV_KEYS if key in env}
    redacted_fields: list[str] = []
    if not redact:
        return source, redacted_fields
    sanitized: dict[str, Any] = {}
    for key, value in source.items():
        if is_sensitive_key(key):
            sanitized[key] = "[REDACTED]"
            redacted_fields.append(key)
        else:
            sanitized[key] = value
    return sanitized, redacted_fields


def is_sensitive_key(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in SENSITIVE_ENV_PARTS)


def render_markdown(payload: dict[str, Any]) -> str:
    run = payload["run"]
    diagnosis = payload.get("diagnosis") or {}
    git = payload.get("git") or {}
    env = payload.get("env") or {}
    config = payload.get("config") or {}
    redacted = payload.get("redacted_fields") or []
    lines = [
        f"# LabGPU Debug Context: {run['name']}",
        "",
        "## Status",
        f"- run_id: `{run['run_id']}`",
        f"- status: `{run['status']}`",
        f"- launch_mode: `{run['launch_mode']}`",
        f"- user: `{run['user']}`",
        f"- host: `{run['host']}`",
        f"- gpu: `{run['gpu'] or ''}`",
        f"- pid: `{run['pid'] or ''}`",
        f"- exit_code: `{run['exit_code'] if run['exit_code'] is not None else ''}`",
        f"- duration_seconds: `{run['duration_seconds'] if run['duration_seconds'] is not None else ''}`",
        "",
        "## Failure",
        f"- detected: `{diagnosis.get('title') or run.get('failure_reason') or 'unknown'}`",
        f"- type: `{diagnosis.get('type') or 'unknown'}`",
        f"- severity: `{diagnosis.get('severity') or ''}`",
        f"- evidence: `{diagnosis.get('evidence') or run.get('failure_evidence') or ''}`",
        f"- suggestion: {diagnosis.get('suggestion') or ''}",
        "",
        "## Command",
        "```bash",
        run["command"],
        "```",
        "",
        "## Working Directory",
        f"`{run['cwd']}`",
        "",
        "## Git",
        f"- branch: `{git.get('git_branch') or ''}`",
        f"- commit: `{git.get('git_commit') or ''}`",
        f"- dirty: `{git.get('git_dirty')}`",
        f"- remote: `{git.get('git_remote') or ''}`",
        "",
        "## Environment",
        f"- python_version: `{env.get('python_version') or ''}`",
        f"- conda_env: `{env.get('conda_env') or ''}`",
        f"- virtual_env: `{env.get('virtual_env') or ''}`",
        f"- CUDA_DEVICE_ORDER: `{env.get('CUDA_DEVICE_ORDER') or ''}`",
        f"- CUDA_VISIBLE_DEVICES: `{env.get('CUDA_VISIBLE_DEVICES') or ''}`",
        f"- redacted_fields: `{', '.join(redacted)}`",
        "",
        "## Config",
        f"- original_paths: `{', '.join(config.get('original_paths') or [])}`",
        f"- snapshot_dir: `{config.get('snapshot_dir') or ''}`",
        f"- snapshot_files: `{', '.join(config.get('snapshot_files') or [])}`",
        "",
        "## Last Logs",
        "```text",
        payload.get("log_tail") or "",
        "```",
    ]
    return "\n".join(lines)
