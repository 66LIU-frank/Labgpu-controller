# Design

LabGPU is a local-first, file-backed personal GPU training workspace. The daily overview is a local UI that reads the student's SSH config; the CLI remains the fast scripting surface.

Core decisions:

- The run directory is the source of truth.
- SQLite, if added later, is only an index.
- `nvidia-smi` is the first real GPU collector.
- A fake collector keeps tests runnable without GPUs.
- `tmux` is the first runner backend.
- Rule-based diagnosis is preferred before any AI or external API integration.
- `meta.json`, `events.jsonl`, `stdout.log`, `env.json`, `git.json`, and `diagnosis.json` are plain files so users can inspect runs without LabGPU.
- `labgpu context` packages local evidence for AI assistants or teammates. The default Assistant mode stays local and rule-based; optional BYO API mode and AI provider integrations must keep secrets local by default and send only redacted workspace context.
- `labgpu refresh` reconciles stale `running` records after wrapper crashes, manual tmux deletion, or server restarts.
- `labgpu ui` / `labgpu servers` is intentionally local-first: it reads the user's SSH config and probes remote machines over SSH, without deploying a daemon.
- `labgpu pick` and the Train Now page share one ranking engine for cross-host GPU recommendations.
- `labgpu where` answers where the user's training is running across SSH hosts.
- `~/.labgpu/config.toml` can store the enabled server inventory, tags, disk paths, shared-account mode, and whether stop-own-process actions are allowed.
- Agentless Mode only requires SSH plus standard tools such as `nvidia-smi`, `ps`, `df`, `free`, and `uptime`.
- Enhanced Mode is opportunistic: if the remote PATH has `labgpu`, the local UI may also show remote LabGPU runs and status. Failure to enter Enhanced Mode must not break Agentless Mode.
- LabGPU Home may stop only the current SSH user's processes. Every stop action re-probes PID identity before signaling and writes a local audit record.
- Process health labels are intentionally conservative. Single-probe idle signals are shown as possible/suspected, not as definitive stuck-process claims.
- Remote AI CLI sessions should prefer session-scoped local gateway tunnels: provider keys remain on the laptop or local vault, SSH reverse forwarding exposes only a temporary loopback endpoint on the remote server, and the gateway requires a per-session token before forwarding to the local provider proxy. Writing provider keys into remote config is an advanced personal-server workflow, not the default.
- The local UI should keep the primary navigation focused on Home, Train, Servers, AI Sessions, and Settings. Secondary tools such as Assistant, Alerts, Groups, and raw JSON views should remain reachable from their related pages without competing with the daily workflow.

The project avoids scheduling, reservations, quotas, and admin panels. It focuses on a student's personal loop: find GPU -> run/adopt -> observe -> diagnose -> context/report -> safe action.
