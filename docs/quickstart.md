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

To view several SSH-configured servers from your laptop:

```bash
labgpu servers --hosts alpha_liu,Song-1
```

Then open `http://127.0.0.1:8787`.
