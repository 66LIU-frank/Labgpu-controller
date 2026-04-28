# LabGPU

> Stop SSH-hopping across lab machines. Open one local page and see which GPU server you can use right now.

LabGPU is a local-first dashboard and CLI for messy shared GPU servers in research labs. It reads your existing `~/.ssh/config`, probes servers over SSH, and shows available GPUs, your own GPU processes, server health, alerts, and safe stop actions.

The basic dashboard does not require a remote daemon, Kubernetes, Slurm, Docker, or a tracking server.

```bash
labgpu ui
```

Open the browser and answer the questions that actually matter:

- Which server has a free A100 / 4090 / H800?
- Which GPU has enough free VRAM for my next run?
- Where are my GPU processes running?
- Is one of my processes idle, failed, or still alive?
- Which server is close to disk full?
- Can I safely stop my own process without touching anyone else's work?

LabGPU is not trying to replace Slurm, Kubernetes, W&B, MLflow, ClearML, or a real cluster scheduler. It is a small tool for the lab reality many students already live in: SSH, tmux, `nvidia-smi`, scattered logs, shared accounts, full disks, and experiments that need to be understood after they fail.

## What You Get

```text
LabGPU Home

Available GPUs
  alpha_liu  GPU 1  A100 80GB      81GB free  0% util
  song_1     GPU 0  RTX 4090       23GB free  0% util

My GPU Processes
  alpha_liu  GPU 5  PID 24988  11h03m  78GB  possible_idle  python sft.py
  song_1     GPU 0  PID 19920  12m     16GB  running        python infer.py

Alerts
  alpha_liu  Disk / is 93.7% used
  alpha_liu  GPU 5 is occupied with low utilization

Servers
  alpha_liu  online  8 x A100      2 free / 8  load 8.9 / 128 cores
  song_1     online  4 x RTX 4090  1 free / 4  healthy
```

LabGPU turns raw GPU process monitoring into a lab-friendly workflow:

```text
GPU process -> experiment name -> logs -> git/config/env -> diagnosis -> report/context
```

## Why It Exists

Small labs often do not have a polished cluster platform. People SSH into the same machines, run training in `tmux` or `nohup`, check `nvidia-smi`, ask who owns a PID, forget where logs went, and discover OOMs the next morning.

LabGPU focuses on that exact gap:

- **One page for many SSH servers**: use the SSH config you already have.
- **Agentless first**: basic server/GPU/process visibility without remote install.
- **Experiment-aware when possible**: if LabGPU exists remotely, show runs, status, diagnosis, and context.
- **Zero-SDK run capsules**: no training-code changes required.
- **Local failure diagnosis**: OOM, NaN, traceback, NCCL, disk full, missing packages.
- **Soft governance**: make resource use visible before adding heavy scheduling rules.

## Two Modes

### Agentless SSH Mode

This is the default mode. LabGPU runs on your laptop and probes servers over SSH.

It collects:

- hostname, uptime, load, CPU cores
- memory, swap, disk usage for common paths
- GPU model, memory, utilization, temperature
- GPU compute processes
- PID, user, runtime, process state, CPU/memory usage, redacted command

No remote LabGPU installation is required.

### Enhanced Mode

If a remote server also has `labgpu` on `PATH`, LabGPU Home tries to fetch:

```bash
labgpu status --json
labgpu list --json
```

That host can then show tracked/adopted/untracked experiments, run names, recent failures, diagnosis, and debug context. If this fails, the UI falls back to Agentless Mode.

## Quick Start

One-command install:

```bash
curl -fsSL https://raw.githubusercontent.com/66LIU-frank/Labgpu-controller/main/install.sh | sh
```

Then start the local dashboard:

```bash
labgpu ui
```

For a specific server from your `~/.ssh/config`:

```bash
labgpu ui --hosts alpha_liu
```

For multiple servers:

```bash
labgpu ui --hosts alpha_liu,song_1,gpu4090
```

Save the server list once, then just run `labgpu ui` later:

```bash
labgpu servers import-ssh --hosts alpha_liu,song_1,gpu4090 --tags lab
labgpu ui
```

Install from source for development:

```bash
git clone git@github.com:66LIU-frank/Labgpu-controller.git labgpu
cd labgpu
python3 -m pip install -e .
labgpu ui
```

For development without installing:

```bash
cd labgpu
PYTHONPATH=src python3 -m labgpu ui --no-open
```

Run against specific SSH aliases:

```bash
PYTHONPATH=src python3 -m labgpu ui --hosts alpha_liu,Song-1
```

Save a server inventory once:

```bash
labgpu servers list
labgpu servers import-ssh --hosts alpha_liu,Song-1 --tags A100,training
labgpu ui
```

The inventory is written to:

```text
~/.labgpu/config.toml
```

LabGPU Home includes a light/dark mode toggle in the top navigation and remembers the preference in the browser.

The `/gpus` page also includes a browser-only "Notify me when GPU is free" watch. It can watch by GPU model, minimum free memory, and server tag. This does not require Telegram, email, Feishu, or any external service.

Example:

```toml
[ui]
refresh_interval_seconds = 15
safe_mode = true

[servers.alpha_liu]
enabled = true
alias = "alpha_liu"
tags = ["A100", "training"]
disk_paths = ["/", "/home", "/data", "/scratch", "/mnt", "/nvme"]
shared_account = false
allow_stop_own_process = true
```

## No-GPU Demo

You can still test the local CLI and fake GPU collector on a laptop:

```bash
labgpu doctor
labgpu status --fake
labgpu status --fake --json
```

## Experiment CLI

The dashboard is the daily entry point, but the CLI still gives you a reproducible experiment workflow.

Launch an experiment in `tmux`:

```bash
labgpu run --name bert_baseline --gpu 0 --config configs/bert.yaml \
  -- python train.py --config configs/bert.yaml
```

Then inspect it:

```bash
labgpu list
labgpu logs bert_baseline --tail 100
labgpu diagnose bert_baseline
labgpu report bert_baseline
labgpu context bert_baseline --tail 200
```

Adopt an already-running process:

```bash
labgpu adopt 23891 --name old_baseline --log ./train.log
```

## Run Capsule

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

Each run can record:

- command, working directory, user, host
- requested GPUs and process metadata
- git commit, branch, dirty state, patch
- selected config files
- Python/Conda/virtualenv details
- stdout/stderr log
- exit code and failure diagnosis
- Markdown report and AI-friendly debug context

Set a shared lab location with:

```bash
export LABGPU_HOME=/shared/labgpu
```

## Safe Stop Actions

LabGPU Home can stop your own remote GPU processes from the UI, but the safety model is deliberately conservative.

Stop actions:

- are only shown for processes owned by the current SSH user
- are disabled for servers marked `shared_account = true` in Agentless Mode
- require a local action token
- re-probe the PID before acting
- verify user, start time, and command hash to reduce PID-reuse risk
- send SIGTERM by default
- only use SIGKILL through an explicit force action
- are disabled when binding outside loopback unless `--allow-actions` is explicitly set
- write an audit record to `~/.labgpu/audit/actions.jsonl`

Alpha does not provide Web run, Web kill-other-users, scheduling, quotas, or reservation features.

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
labgpu report RUN [--json]
labgpu context RUN [--format markdown|json] [--tail 200]
labgpu adopt PID --name NAME [--log train.log]

labgpu ui [--hosts alpha_liu,Song-1] [--pattern Sui]
labgpu ui --host 0.0.0.0 --allow-actions   # only if you explicitly accept the risk

labgpu servers [--hosts alpha_liu,Song-1] [--pattern Sui]
labgpu servers list
labgpu servers probe alpha_liu
labgpu servers probe --all --json
labgpu servers import-ssh --hosts alpha_liu,Song-1 --tags A100,training

labgpu web [--host 127.0.0.1] [--port 8765]
```

`labgpu ui` is the multi-server SSH dashboard. `labgpu web` is the older single-machine dashboard for the current machine's LabGPU runs.

## Privacy

LabGPU is designed for shared lab environments, so it avoids exposing sensitive data by default.

- Commands are truncated and redacted in LabGPU Home.
- Sensitive-looking arguments containing `TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `PASSWD`, `OPENAI_API_KEY`, `WANDB_API_KEY`, `HF_TOKEN`, `GITHUB_TOKEN`, `AWS_SECRET_ACCESS_KEY`, and similar terms are redacted.
- Full environment variables are not shown in the UI.
- `labgpu context` uses a safe environment subset by default.
- `--include-env` still applies redaction unless `--no-redact` is explicitly used.
- Web servers bind to `127.0.0.1` by default.

Keep `labgpu web` behind SSH tunneling unless you add your own access control.

## Real-Server Validation

On your laptop:

```bash
labgpu servers list
labgpu servers probe alpha_liu
labgpu ui --hosts alpha_liu
```

On a GPU server:

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

## Current Status

This repository is an Alpha. The current focus is real usability on lab servers, not feature breadth.

Implemented areas include:

- fake and real NVIDIA GPU status collection
- file-backed run metadata
- tmux-based experiment launch
- log capture and tailing
- run refresh/reconciliation
- safe kill for LabGPU runs
- rule-based diagnosis
- Markdown reports and debug context
- `adopt` for existing PIDs
- single-machine `labgpu web`
- multi-server `labgpu ui` / `servers` dashboard
- SSH inventory import, cache, alerts, available GPU view, my-process view, and safe stop-own-process actions

Known limitations:

- No scheduler, queue, preemption, quota, or reservation system.
- Real GPU status currently targets NVIDIA via `nvidia-smi`.
- Process health labels are conservative. `possible_idle` means the probe saw occupied GPU memory with low utilization; it is not a definitive stuck-process diagnosis.
- Agentless Mode can only infer ownership from the SSH user and Linux process owner. For shared Linux accounts, disable stop actions or use Enhanced Mode with LabGPU-tracked runs.
- The Web UI has no full authentication layer in Alpha; keep it local.

## Roadmap

- v0.1: `doctor`, `status`, fake GPU collector.
- v0.2: file-backed run metadata, `list`.
- v0.3: `run` with tmux, log capture, exit code.
- v0.4: `logs`, `kill`, status-to-run mapping.
- v0.5: rule-based diagnosis.
- v0.6: Web dashboard.
- v0.7: `adopt`, Markdown reports, debug context, shared `LABGPU_HOME`.
- next: harden LabGPU Home, improve process-health history, polish onboarding, and validate with real lab users.

LabGPU will stay intentionally small until the basic loop is boringly reliable: open a page, find a GPU, understand your processes, diagnose failures, and avoid hurting anyone else's work.
