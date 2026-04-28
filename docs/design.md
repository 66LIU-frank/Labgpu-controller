# Design

LabGPU is local-first and file-backed. The daily overview is a local UI that reads SSH config; the CLI remains the scripting and advanced-operation surface.

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
- `labgpu ui` / `labgpu servers` is intentionally local-first: it reads the user's SSH config and probes remote machines over SSH, without deploying a daemon.
- Agentless Mode only requires SSH plus standard tools such as `nvidia-smi`, `ps`, `df`, `free`, and `uptime`.
- Enhanced Mode is opportunistic: if the remote PATH has `labgpu`, the local UI may also show remote LabGPU runs and status. Failure to enter Enhanced Mode must not break Agentless Mode.

The project avoids heavyweight scheduling. It focuses on visibility, reproducibility, diagnosis, and a low-friction workflow.
