# Differentiation

LabGPU is not another lab admin dashboard and not a lightweight Slurm clone.

Its wedge is:

```text
personal remote GPU training workspace for students
```

It links:

```text
find GPU -> run/adopt -> observe -> diagnose -> context/report -> safe action
```

## What Is Not Novel

- GPU memory/utilization monitoring.
- Web GPU dashboards.
- GPU reservation calendars.
- Single-node or cluster scheduling.
- Full MLOps tracking.

These are already covered by adjacent tools such as gpustat, nvitop, gpuview, TensorHive, gflow, MLflow, ClearML, DVC, and Aim.

## What LabGPU Should Own

### Experiment-Aware GPU Status

Status should show `free`, `tracked`, `adopted`, `untracked`, and `unknown`, not just PID and memory.

### Cross-Host Pick

`labgpu pick` should recommend a GPU across SSH hosts using free VRAM, model, load, disk health, alerts, and tags. This differs from single-host tools such as `nvidia-smi`, `gpustat`, `nvitop`, or local GPU selectors.

### Adopt Existing Processes

Students already have tmux/nohup/screen jobs. `labgpu adopt <pid> --name exp` turns an existing process into a run record without forcing a restart.

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

### Safe Personal Actions

Do not start with quotas, reservations, preemption, or admin panels. Start with personal visibility: where my run is, how long it has run, whether it is tracked, and why it failed.

### AI Debug Context

`labgpu context RUN` exports a Markdown or JSON context pack that can be pasted into an AI assistant, issue, or message to a teammate.

It should not call external APIs. It only packages local evidence.

## Differentiation Checklist

- `status` shows `tracking_status`.
- `pick` recommends across SSH hosts.
- `where` shows where my training is running.
- `adopt` creates `launch_mode=adopted`.
- Untracked processes show an adopt hint.
- Run folders are inspectable without LabGPU.
- `diagnose` stores structured evidence.
- `report` is human-readable.
- `context` is optimized for AI and teammate debugging.
- Web starts with Train Now, My Runs, Failed/Suspicious Runs, and Problems; Servers is resource detail.
- Sensitive environment data is not dumped by default.
