# Design

LabGPU is CLI-first and file-backed.

Core decisions:

- The run directory is the source of truth.
- SQLite, if added later, is only an index.
- `nvidia-smi` is the first real GPU collector.
- A fake collector keeps tests runnable without GPUs.
- `tmux` is the first runner backend.
- Rule-based diagnosis is preferred before any AI or external API integration.
- `meta.json`, `events.jsonl`, `stdout.log`, `env.json`, `git.json`, and `diagnosis.json` are plain files so users can inspect runs without LabGPU.
- `labgpu context` packages local evidence for AI assistants or teammates, but LabGPU does not call external APIs.
- `labgpu refresh` reconciles stale `running` records after wrapper crashes, manual tmux deletion, or server restarts.
- `labgpu servers` is intentionally local-first: it reads the user's SSH config and probes remote machines over SSH, without deploying a daemon.

The project avoids heavyweight scheduling. It focuses on visibility, reproducibility, diagnosis, and a low-friction workflow.
