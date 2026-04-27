from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


RUN_STATUSES = {
    "created",
    "running",
    "success",
    "failed",
    "killed",
    "orphaned",
    "unknown",
}


@dataclass
class RunMeta:
    run_id: str
    name: str
    user: str
    host: str
    status: str
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: int | None = None
    command: str = ""
    command_argv: list[str] = field(default_factory=list)
    cwd: str = ""
    shell: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    requested_gpu_indices: list[str] = field(default_factory=list)
    gpu_uuids: list[str] = field(default_factory=list)
    cuda_visible_devices: str | None = None
    pid: int | None = None
    process_group_id: int | None = None
    tmux_session: str | None = None
    log_path: str | None = None
    stderr_path: str | None = None
    git_commit: str | None = None
    git_branch: str | None = None
    git_dirty: bool | None = None
    git_remote: str | None = None
    git_patch_path: str | None = None
    git_json_path: str | None = None
    config_paths: list[str] = field(default_factory=list)
    config_snapshot_dir: str | None = None
    python_version: str | None = None
    conda_env: str | None = None
    virtual_env: str | None = None
    env_json_path: str | None = None
    exit_code: int | None = None
    failure_reason: str | None = None
    failure_evidence: str | None = None
    launch_mode: str = "labgpu"
    project: str | None = None
    tags: list[str] = field(default_factory=list)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunMeta":
        fields = cls.__dataclass_fields__
        return cls(**{key: data.get(key) for key in fields if key in data})
