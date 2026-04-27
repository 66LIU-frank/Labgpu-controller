# Differentiation

LabGPU is not another GPU dashboard and not a lightweight Slurm clone.

Its wedge is:

```text
messy-lab-first experiment observability
```

It links:

```text
GPU process -> experiment name -> logs -> git/config/env -> failure diagnosis -> reproducible report/debug context
```

## What Is Not Novel

- GPU memory/utilization monitoring.
- Web GPU dashboards.
- GPU reservation calendars.
- Single-node GPU scheduling.
- Full MLOps tracking.

These are already covered by adjacent tools such as gpustat, nvitop, gpuview, TensorHive, gflow, MLflow, ClearML, DVC, and Aim.

## What LabGPU Should Own

### Experiment-Aware GPU Status

Status should show `free`, `tracked`, `adopted`, `untracked`, and `unknown`, not just PID and memory.

### Adopt Existing Processes

Labs already have tmux/nohup/screen jobs. `labgpu adopt <pid> --name exp` turns an existing process into a run record without forcing users to restart it.

### Zero-SDK Run Capsule

`labgpu run` creates a portable run folder:

```text
meta.json
events.jsonl
stdout.log
command.sh
env.json
git.json
git.patch
config/
diagnosis.json
```

No training-code SDK is required.

### Local Failure Diagnosis

LabGPU should explain common failures from logs: CUDA OOM, NaN/Inf, Traceback, Killed, disk full, missing module, import error, permission issue, port conflict, and NCCL error.

### Soft Governance

Do not start with quotas, reservations, or preemption. Start with transparency: who is running what, how long it has run, whether it is tracked, and why it failed.

### AI Debug Context

`labgpu context RUN` exports a Markdown or JSON context pack that can be pasted into an AI assistant, issue, or message to a teammate.

It should not call external APIs. It only packages local evidence.

## Differentiation Checklist

- `status` shows `tracking_status`.
- `adopt` creates `launch_mode=adopted`.
- Untracked processes show an adopt hint.
- Run folders are inspectable without LabGPU.
- `diagnose` stores structured evidence.
- `report` is human-readable.
- `context` is optimized for AI and teammate debugging.
- Web focuses on experiments, not only GPU cards.
- Sensitive environment data is not dumped by default.
