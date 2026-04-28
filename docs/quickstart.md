# Quickstart

Use LabGPU from an SSH session on a shared GPU server.

```bash
labgpu doctor
labgpu status
labgpu run --name baseline --gpu 0 -- python train.py --config configs/base.yaml
labgpu refresh
labgpu logs baseline --tail 100
labgpu diagnose baseline
labgpu context baseline
```

Each run gets a directory under `~/.labgpu/runs/`. The most important files are `meta.json`, `stdout.log`, `events.jsonl`, and `command.sh`.

For demos or development machines without NVIDIA GPUs:

```bash
labgpu status --fake
labgpu web --fake
```

To view several SSH-configured servers from your laptop, use LabGPU Home:

```bash
labgpu ui --hosts alpha_liu,Song-1
```

Then open `http://127.0.0.1:8765`. This uses your local `~/.ssh/config`; do not run it from inside the remote server shell.

The home page shows server cards, available GPUs, your own GPU processes, and alerts. Stop buttons are only shown for processes owned by the current SSH user and are guarded by a re-probe before any signal is sent.

To save your server list once:

```bash
labgpu servers import-ssh --hosts alpha_liu,Song-1 --tags A100,training
labgpu ui
```

For CLI debugging:

```bash
labgpu servers list
labgpu servers probe alpha_liu
```
