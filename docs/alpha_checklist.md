# LabGPU Alpha Checklist

Alpha is intentionally narrow:

- `labgpu doctor`
- `labgpu status`, `status --json`, `status --fake`, `status --watch`
- `labgpu refresh` for stale running state
- GPU collector and fake collector
- PID to user, command, cwd, create time, and permission-aware process inspection
- file-backed run storage with `meta.json`, `events.jsonl`, `stdout.log`, `command.sh`, `env.json`, `git.json`, `diagnosis.json`
- `labgpu run` with tmux and `CUDA_DEVICE_ORDER=PCI_BUS_ID`
- `labgpu list`
- `labgpu logs --tail/--follow`
- `labgpu kill` for the current user's own runs
- `labgpu diagnose` with local regex rules
- `labgpu report`
- `labgpu context` for AI/teammate debug packages
- `labgpu web` with local dashboard and JSON APIs
- no-GPU testability through fake data
- ambiguous `kill` targets are rejected
- context/log output is size-limited and redacted by default

Alpha explicitly does not include queues, scheduling, reservations, Docker orchestration, Kubernetes, Slurm integration, multi-host aggregation, Web login, Web run, or Web kill.
