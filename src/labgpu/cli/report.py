from __future__ import annotations

import json
from pathlib import Path

from labgpu.cli.resolve import resolve_run
from labgpu.core.store import RunStore


def run(args) -> int:
    store = RunStore()
    meta = resolve_run(store, args.ref)
    if args.json:
        print(json.dumps(meta.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    print(render_report(meta))
    return 0


def render_report(meta) -> str:
    lines = [
        f"# Experiment Report: {meta.name}",
        "",
        f"- Run ID: `{meta.run_id}`",
        f"- Status: `{meta.status}`",
        f"- User: `{meta.user}`",
        f"- Host: `{meta.host}`",
        f"- GPU: `{meta.cuda_visible_devices or ','.join(meta.requested_gpu_indices)}`",
        f"- Duration: `{meta.duration_seconds or 0}s`",
        f"- Command: `{meta.command}`",
        f"- CWD: `{meta.cwd}`",
        f"- Git branch: `{meta.git_branch or ''}`",
        f"- Git commit: `{meta.git_commit or ''}`",
        f"- Git dirty: `{meta.git_dirty}`",
        f"- Log: `{meta.log_path or ''}`",
    ]
    if meta.failure_reason:
        lines.extend(["", "## Diagnosis", f"{meta.failure_reason}"])
        if meta.failure_evidence:
            lines.append(meta.failure_evidence)
    lines.extend(["", "## Last Logs", "```text", _tail(meta.log_path), "```"])
    return "\n".join(lines)


def _tail(path: str | None) -> str:
    if not path or not Path(path).exists():
        return ""
    data = Path(path).read_bytes()
    return b"\n".join(data.splitlines()[-80:]).decode(errors="replace")
