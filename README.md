# LabGPU

LabGPU is a lightweight experiment observability layer for messy shared GPU servers in research labs.

It helps students and lab mates answer the boring but important questions:

- Who is using GPU 0?
- Which experiment is this PID?
- Where are the logs?
- Did my experiment finish or crash?
- Was it CUDA OOM, NaN, NCCL, disk full, or a missing package?
- Which git commit and config produced this run?

LabGPU is not another GPU dashboard and not a Slurm, Kubernetes, Docker, W&B, MLflow, or ClearML replacement. It is a local-first lab console plus CLI that connects the workflow many labs already use: SSH, tmux, `nvidia-smi`, git, config files, local logs, failure diagnosis, and reproducible debug context.

## Differentiation

LabGPU links:

```text
GPU process -> experiment name -> logs -> git/config/env -> diagnosis -> report/context
```

The core ideas are:

- experiment-aware GPU status, not just hardware metrics
- `adopt` for existing tmux/nohup/screen processes
- zero-SDK run capsules
- local failure diagnosis
- soft governance instead of hard scheduling
- one-command debug context for AI assistants and teammates

## Status

This repository is an early MVP. The first implementation is intentionally small and dependency-light so it can run on development machines without NVIDIA GPUs. Real GPU collection uses `nvidia-smi`; demos and tests use `labgpu status --fake`.

## Install From Source

```bash
git clone <your-fork-url> labgpu
cd labgpu
python3 -m pip install -e .
```

For development without installing:

```bash
PYTHONPATH=src python3 -m labgpu --help
```

## Quick Demo

```bash
labgpu doctor
labgpu status --fake
labgpu status --fake --json
labgpu ui --no-open
```

On a GPU server:

```bash
labgpu status

labgpu run --name bert_baseline --gpu 0 \
  -- python train.py --config configs/bert.yaml

labgpu list
labgpu logs bert_baseline --tail 100
labgpu diagnose bert_baseline
labgpu context bert_baseline
labgpu web
```

The Web dashboard listens on `127.0.0.1:8765` by default. Use SSH tunneling from your laptop:

```bash
ssh -L 8765:localhost:8765 user@gpu-server
```

Then open `http://127.0.0.1:8765`.

## Data Model

LabGPU treats the run directory as the source of truth:

```text
~/.labgpu/runs/<run_id>/
  meta.json
  events.jsonl
  stdout.log
  command.sh
  env.json
  git.json
  config/
  git.patch
  diagnosis.json
```

There is no required database in the MVP. A future SQLite index can be rebuilt from these files.

Set a shared lab location with:

```bash
export LABGPU_HOME=/shared/labgpu
```

## Commands

```text
labgpu doctor
labgpu status [--json] [--fake] [--watch]
labgpu refresh
labgpu run --name NAME --gpu 0 -- COMMAND ...
labgpu list [--all] [--user USER] [--status failed] [--json]
labgpu logs RUN [--tail 100] [--follow]
labgpu kill RUN [--force]
labgpu diagnose RUN
labgpu report RUN
labgpu context RUN [--format markdown|json]
labgpu adopt PID --name NAME [--log train.log]
labgpu web [--host 127.0.0.1] [--port 8765]
labgpu ui [--hosts alpha_liu,Song-1] [--pattern Sui]
labgpu ui --host 0.0.0.0 --allow-actions   # only if you explicitly accept the risk
labgpu servers [--hosts alpha_liu,Song-1] [--pattern Sui]
labgpu servers list
labgpu servers probe alpha_liu
```

## LabGPU Home

`labgpu ui` runs on your laptop, reads your local `~/.ssh/config`, and probes configured servers over SSH. It does not require LabGPU to be installed on the remote servers.

```bash
labgpu ui --hosts alpha_liu,Song-1
```

Open:

```text
http://127.0.0.1:8765
```

This is Agentless Mode: SSH is enough to show hostname, uptime, CPU load, memory, swap, disk mounts, GPUs, GPU processes, process runtime/state, users, available GPUs, your own GPU processes, alerts, and redacted commands. If `labgpu` is also available on the remote server PATH, LabGPU Home switches that host to Enhanced Mode and attempts to show remote LabGPU status and runs.

LabGPU Home can safely stop your own remote GPU processes from the UI. Stop actions:

- are only shown for processes owned by the current SSH user
- require a local action token
- re-probe the PID and verify user, start time, and command hash before sending a signal
- send SIGTERM by default; Force sends SIGKILL
- are disabled when binding outside loopback unless `--allow-actions` is explicitly set
- are recorded in `~/.labgpu/audit/actions.jsonl`

For lower-level server debugging:

```bash
labgpu servers list
labgpu servers probe alpha_liu
labgpu servers --hosts alpha_liu --json
```

This is different from `labgpu web`: `web` shows the current machine's LabGPU runs, while `ui` / `servers` is a local SSH overview page for many machines.

## Alpha Limitations

- LabGPU does not schedule jobs, reserve GPUs, preempt processes, or enforce quotas.
- `status` relies on `nvidia-smi` for real NVIDIA GPUs.
- `run` relies on `tmux`; if tmux is unavailable, `doctor` will warn.
- Running-state reconciliation is best effort. Use `labgpu refresh` if a server reboot or wrapper crash leaves stale runs.
- `kill` refuses ambiguous names; use the full `run_id` when multiple runs match.
- The Web dashboard has no authentication in Alpha and binds to `127.0.0.1` by default.
- Process health labels are conservative. `possible_idle` means the current probe saw occupied GPU memory with low GPU utilization; it is not a definitive stuck-process diagnosis.

## Privacy

`labgpu context` is designed for sharing with AI assistants or teammates, so it uses a safe environment subset by default. Sensitive environment names containing `TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `WANDB`, `HF`, `OPENAI`, `GITHUB`, `AWS`, and similar terms are redacted when full env output is requested with `--include-env`.

Web/API output does not expose full environment files by default. LabGPU Home also redacts sensitive-looking command arguments such as tokens, keys, secrets, and passwords. Keep `labgpu web` behind SSH tunneling unless you add your own access control. Do not enable UI actions on `0.0.0.0` unless you understand the exposure.

## Real-Server Validation

After installing on a GPU server:

```bash
labgpu doctor
labgpu status
labgpu run --name smoke_success --gpu 0 -- bash -lc 'echo start; sleep 1; echo done'
sleep 2
labgpu refresh
labgpu list --all
labgpu logs smoke_success --tail 20
labgpu context smoke_success --tail 20
```

For a fuller Alpha check from source:

```bash
LABGPU_BIN="python3 -m labgpu" PYTHONPATH=src scripts/alpha_smoke_test.sh
```

## Roadmap

- v0.1: `doctor`, `status`, fake GPU collector.
- v0.2: file-backed run metadata, `list`.
- v0.3: `run` with tmux, log capture, exit code.
- v0.4: `logs`, `kill`, status-to-run mapping.
- v0.5: rule-based diagnosis.
- v0.6: Web dashboard.
- v0.7: `adopt`, Markdown reports, debug context, shared `LABGPU_HOME`.

The project will deliberately avoid scheduling, quotas, reservation calendars, preemption, Docker orchestration, Kubernetes, and Slurm replacement features until the basic experiment lifecycle is reliable.
