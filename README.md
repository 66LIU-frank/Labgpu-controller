# LabGPU

> Personal GPU workspace for students using shared SSH servers.
>
> Find a free GPU. Launch training. Track your runs. Diagnose failures.
>
> No daemon. No root. No Slurm. No Kubernetes.

```bash
labgpu ui
labgpu pick --min-vram 24G --prefer A100
labgpu run --name sft --gpu 0 -- python train.py --config configs/sft.yaml
labgpu where
```

![LabGPU Home demo preview](docs/assets/labgpu-home-demo.svg)

LabGPU is a personal remote GPU training workspace for students and individual researchers who use several shared SSH GPU servers without admin privileges. It reads your existing `~/.ssh/config`, probes servers over SSH, recommends a GPU, tracks your own runs, saves logs and reproducibility context, diagnoses common failures, and helps you safely stop your own processes.

The basic workflow does not require a remote daemon, root access, Slurm, Kubernetes, Docker, or a tracking server.

```bash
labgpu ui
```

No GPU servers handy? Launch the built-in multi-server demo:

```bash
labgpu demo
```

Open the browser or CLI and answer the questions that matter before and after a training run:

- Which SSH host has a GPU with enough free VRAM?
- What command should I copy to start training there?
- Where are my runs and untracked GPU processes?
- What failed overnight: OOM, traceback, NCCL, disk full, or something else?
- What context should I paste into an AI assistant or send to a teammate?
- Can I safely stop my own process without touching anyone else's work?

LabGPU is not trying to replace Slurm, Kubernetes, W&B, MLflow, ClearML, or a real scheduler. It is a personal workspace for the reality many students already live in: SSH aliases, tmux, `nvidia-smi`, scattered logs, full disks, and training runs that need to be found and understood after they fail.

## What LabGPU Is Not

LabGPU is not:

- a scheduler
- a reservation calendar
- a quota system
- an admin panel
- a replacement for Slurm or Kubernetes
- a replacement for W&B, MLflow, or ClearML
- a tool for managing other people's jobs

By default, LabGPU is personal. It helps you find GPUs, track your own training, export debug context, and only take safe actions on your own processes.

## What You Get

```text
LabGPU Home - Personal Training Workspace

Train Now / Recommended GPUs
  Recommended  alpha_liu  GPU 0  A100 80GB      80GB free  copy ssh / CUDA / launch
  OK           song_1     GPU 0  RTX 4090       23GB free  copy ssh / CUDA / launch

My Runs
  alpha_liu  sft_retry      running   GPU 0  PID 24988  tail log / diagnose / context
  song_1     old_baseline   adopted   GPU 1  PID 19920  copy command / stop safely

Failed or Suspicious Runs
  alpha_liu  pretrain_0428  failed    CUDA out of memory
  song_1     PID 19920      warning   suspected idle

Servers
  alpha_liu  online  8 x A100      2 free / 8  resource details
  song_1     online  4 x RTX 4090  1 free / 4  healthy
```

LabGPU turns raw GPU process monitoring into a personal training workflow:

```text
find GPU -> run/adopt -> observe -> diagnose -> context/report -> safe action
```

## Why It Exists

Students often use several shared GPU servers without the power to install cluster software. They SSH into machines, run training in `tmux` or `nohup`, check `nvidia-smi`, forget where logs went, and discover OOMs the next morning.

LabGPU focuses on that exact gap:

- **One personal page for many SSH servers**: use the SSH config you already have.
- **Agentless first**: basic server/GPU/process visibility without remote install.
- **Experiment-aware when possible**: if LabGPU exists remotely, show your runs, status, diagnosis, and context.
- **Zero-SDK run capsules**: no training-code changes required.
- **Local failure diagnosis**: OOM, NaN, traceback, NCCL, disk full, missing packages.
- **Safe personal actions**: copy commands, adopt your processes, and stop only your own work.

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

Then start your personal training workspace:

```bash
labgpu ui
```

From the terminal, ask LabGPU where to train:

```bash
labgpu pick --min-vram 24G --prefer A100 --tag training
labgpu pick --min-vram 24G --prefer 4090 --cmd
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

This saved list becomes the default LabGPU Home probe set. Use it to keep the
home page focused and fast:

```bash
labgpu servers import-ssh --hosts alpha_liu,alpha_shi --tags A100,training
labgpu ui
```

You can also choose the same list from **Settings -> Save selected hosts** in
LabGPU Home. Servers that are not selected stay available in your SSH config,
but they are not probed on every home-page refresh.

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

LabGPU Home includes Chinese/English and light/dark mode toggles in the top navigation and remembers those preferences in the browser.

If a saved server cannot be reached, LabGPU keeps it visible as
`offline · cached` when a previous successful probe exists. The card shows the
current SSH error and labels GPU, disk, load, and process counts as cached, so a
timeout does not make a server disappear or look fresher than it is.

The Train Now page (`/gpus`) ranks GPUs as `Recommended`, `OK`, `Busy`, or `Not recommended` using free VRAM, model, server load, disk health, alerts, and tags. Each GPU card includes copy buttons for the SSH command, `CUDA_VISIBLE_DEVICES`, and a launch snippet.

The same recommendation model is available from the terminal:

```bash
labgpu pick --prefer A100 --min-vram 40G
labgpu pick --fake-lab
```

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

You can still test the local CLI, fake GPU collector, and multi-server Home UI on a laptop:

```bash
labgpu doctor
labgpu status --fake
labgpu status --fake --json
labgpu demo
labgpu ui --fake-lab
labgpu pick --fake-lab
```

## Experiment CLI

LabGPU Home is the daily entry point, but the CLI still gives you a reproducible experiment workflow.

Launch an experiment in `tmux`:

```bash
labgpu run --name bert_baseline --gpu 0 --config configs/bert.yaml \
  -- python train.py --config configs/bert.yaml
```

Then inspect it:

```bash
labgpu list
labgpu where
labgpu logs bert_baseline --tail 100
labgpu diagnose bert_baseline
labgpu context bert_baseline --tail 200 --copy
labgpu report bert_baseline
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

Most users should keep the default personal `~/.labgpu`. Shared `LABGPU_HOME` is advanced and can expose metadata to other users; if a group explicitly wants it, use a group-owned directory rather than a world-writable directory. See `docs/security.md` and `docs/lab_setup.md`.

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

LabGPU does not provide scheduling, reservations, quotas, admin panels, or kill-other-users actions.

## Commands

```text
labgpu doctor
labgpu status [--json] [--fake] [--watch]
labgpu pick [--min-vram 24G] [--prefer A100] [--tag training] [--cmd] [--json]
labgpu where [--json]
labgpu refresh
labgpu run --name NAME --gpu 0 -- COMMAND ...
labgpu list [--all] [--user USER] [--status failed] [--json]
labgpu logs RUN [--tail 100] [--follow]
labgpu kill RUN [--force]
labgpu diagnose RUN
labgpu report RUN [--json]
labgpu context RUN [--format markdown|json] [--tail 200] [--copy]
labgpu adopt PID --name NAME [--log train.log]

labgpu ui [--hosts alpha_liu,Song-1] [--pattern Sui]
labgpu ui --fake-lab
labgpu demo
labgpu ui --host 0.0.0.0 --allow-actions   # only if you explicitly accept the risk

labgpu servers [--hosts alpha_liu,Song-1] [--pattern Sui]
labgpu servers list
labgpu servers probe alpha_liu
labgpu servers probe --all --json
labgpu servers import-ssh --hosts alpha_liu,Song-1 --tags A100,training

labgpu web [--host 127.0.0.1] [--port 8765]
```

`labgpu ui` is the personal multi-server SSH workspace. `labgpu web` is the older single-machine dashboard for the current machine's LabGPU runs.

## Privacy

LabGPU is designed for personal use on shared SSH servers, so it avoids exposing sensitive data by default.

- Commands are truncated and redacted in LabGPU Home.
- Sensitive-looking arguments containing `TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `PASSWD`, `OPENAI_API_KEY`, `WANDB_API_KEY`, `HF_TOKEN`, `GITHUB_TOKEN`, `AWS_SECRET_ACCESS_KEY`, and similar terms are redacted.
- Full environment variables are not shown in the UI.
- `labgpu context` uses a safe environment subset by default.
- `--include-env` still applies redaction unless `--no-redact` is explicitly used.
- Web servers bind to `127.0.0.1` by default.

Keep `labgpu web` behind SSH tunneling unless you add your own access control.
Read `docs/security.md` before enabling shared `LABGPU_HOME`, shared-account
servers, or remote-facing UI binds.

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

This repository is an Alpha. The current focus is fast personal training workflow on shared SSH servers, not admin feature breadth.

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
- personal multi-server `labgpu ui` workspace
- SSH inventory import, cache, alerts, available GPU view, my-process view, and safe stop-own-process actions
- GPU recommendation model and `labgpu pick`
- `labgpu where` for finding your training quickly
- fake multi-server demo via `labgpu demo` / `labgpu ui --fake-lab`

Known limitations:

- No scheduler, queue, preemption, quota, reservation system, or admin panel.
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
- next: harden the personal workspace, improve failure inbox and process-health history, polish onboarding, and validate with student users.

LabGPU will stay intentionally small until the basic student loop is boringly reliable: find a GPU, start or adopt training, find your work later, diagnose failures, export context, and avoid hurting anyone else's process.
