# LabGPU

[![CI](https://github.com/66LIU-frank/Labgpu-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/66LIU-frank/Labgpu-controller/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-v0.1.0--alpha-orange)

[English](README.md) | [简体中文](README.zh-CN.md)

Personal GPU workspace for students using shared SSH servers.

Find a free GPU. Launch training. Track your runs. Diagnose failures.
No daemon. No root. No Slurm. No Kubernetes.

![LabGPU Home demo preview](docs/assets/labgpu-home-demo.svg)

```text
find GPU -> run/adopt -> observe -> diagnose -> context/report -> safe action
```

## Start Fast

Try the fake multi-server demo on any laptop:

```bash
pipx install git+https://github.com/66LIU-frank/Labgpu-controller.git
labgpu demo
labgpu pick --fake-lab
```

Use your real SSH GPU servers:

```bash
labgpu init --hosts alpha_liu,alpha_shi --tags A100,training
labgpu ui
labgpu pick --min-vram 24G --prefer A100
```

On the chosen GPU server:

```bash
labgpu run --name sft --gpu auto --min-vram 24G -- python train.py --config configs/sft.yaml
labgpu where
```

## What It Does

| Need | LabGPU gives you |
| --- | --- |
| Find a usable GPU | `Train Now` and `labgpu pick` rank GPUs across SSH hosts. |
| Start training quickly | Copy SSH, `CUDA_VISIBLE_DEVICES`, launch snippets, or open an SSH terminal from the GPU card. |
| Find your own jobs | `My Runs` and `labgpu where` show tracked, adopted, and own GPU processes. |
| Recover experiment context | Run capsules save command, log, git, config, env summary, and GPU info. |
| Debug failures | `diagnose` and Failure Inbox catch OOM, traceback, NCCL, disk full, killed, NaN, and suspected idle. |
| Ask AI or teammates for help | `labgpu context --copy` exports one redacted Markdown debug context. |
| Chat with your workspace | `LabGPU Assistant` answers from current GPU/runs/failure data and returns copyable plans. |
| Stop safely | UI actions only target your own process, with conservative checks. |

## Daily Workflow

Install:

```bash
pipx install git+https://github.com/66LIU-frank/Labgpu-controller.git
```

Or:

```bash
curl -fsSL https://raw.githubusercontent.com/66LIU-frank/Labgpu-controller/main/install.sh | sh
```

Choose the SSH hosts shown on your homepage:

```bash
labgpu init
labgpu init --hosts alpha_liu,alpha_shi --tags A100,training
```

Open the workspace:

```bash
labgpu ui
```

Find a GPU:

```bash
labgpu pick --min-vram 24G --prefer A100 --tag training --explain
labgpu pick --min-vram 24G --prefer 4090 --cmd "python train.py --config configs/sft.yaml"
```

Launch or adopt training on a GPU server:

```bash
labgpu run --name baseline --gpu auto --min-vram 24G -- python train.py
labgpu adopt 23891 --name old_baseline --log ./train.log
```

Find and debug your work:

```bash
labgpu where
labgpu logs baseline --tail 100
labgpu diagnose baseline
labgpu context baseline --copy
labgpu report baseline
```

## UI Layout

LabGPU Home is training-first:

```text
Train Now
  Recommended GPUs ranked by free VRAM, model, load, disk health, alerts, and tags.
  Each card can copy commands or open an SSH terminal for that server.

My Runs
  LabGPU runs, adopted runs, and own untracked GPU processes.

Failed or Suspicious Runs
  OOM, traceback, NCCL, disk full, killed, NaN, suspected idle, and stale logs.

Assistant
  Read-only chat for GPU recommendations, where-is-my-run answers, failure summaries, and copyable launch/debug plans.

Problems
  Offline/cached servers, disk warnings, probe timeouts, and process health warnings.

Servers
  Resource details stay below the main workflow.
```

The UI supports Chinese/English and light/dark mode. Pages load from local snapshots first, then refresh stale SSH data in the background, so moving between pages does not wait on slow SSH probes.

## Run Capsule

Each tracked or adopted run gets a directory under `~/.labgpu/runs/`:

```text
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

This records the useful debugging surface: command, cwd, user, host, GPU, PID, logs, git commit/patch, selected configs, Python/Conda/venv summary, exit code, diagnosis, and Markdown context.

## Modes

Agentless SSH Mode is the default. LabGPU runs on your laptop, reads `~/.ssh/config`, and probes servers over SSH. No remote install is needed for GPU/process visibility.

Enhanced Mode is optional. If the remote server has `labgpu` on `PATH`, LabGPU Home also reads:

```bash
labgpu status --json
labgpu list --json
```

That enables richer tracked/adopted run details. If it fails, the UI falls back to Agentless Mode.

## Safety

LabGPU is personal-first. It is not a scheduler, reservation system, quota system, admin panel, Slurm/Kubernetes replacement, or a tool for managing other people's jobs.

Safe stop actions:

- only show for processes owned by the current SSH user
- are disabled for shared Linux accounts unless configured otherwise
- re-probe PID/user/start time/command hash before acting
- send SIGTERM by default
- require explicit force for SIGKILL
- are disabled outside loopback unless `--allow-actions` is set

Commands and debug context are redacted by default. Shared `LABGPU_HOME` is advanced because it can expose metadata to other users; see [docs/security.md](docs/security.md) and [docs/lab_setup.md](docs/lab_setup.md).

## Commands

```text
labgpu init [--hosts alpha_liu,alpha_shi] [--tags A100,training]
labgpu ui [--hosts alpha_liu,alpha_shi] [--fake-lab]
# Browser: /assistant opens LabGPU Assistant.
labgpu pick [--min-vram 24G] [--prefer A100] [--tag training] [--explain] [--cmd "COMMAND"] [--json]
labgpu where [--json]

labgpu run --name NAME --gpu 0|auto [--min-vram 24G] -- COMMAND ...
labgpu adopt PID --name NAME [--log train.log]
labgpu list [--all] [--json]
labgpu logs RUN [--tail 100] [--follow]
labgpu diagnose RUN
labgpu context RUN [--copy] [--format markdown|json]
labgpu report RUN [--json]
labgpu kill RUN [--force]

labgpu status [--json] [--fake] [--watch]
labgpu servers list
labgpu servers probe alpha_liu
labgpu demo
```

## Status

LabGPU is alpha. It currently targets NVIDIA servers via `nvidia-smi`, SSH aliases, tmux-based local run launch, run capsules, GPU ranking, Failure Inbox, debug context export, and safe own-process actions.

Known boundaries:

- no scheduler, queue, reservation, quota, or admin panel
- no full authentication layer for public-facing web use
- shared Linux accounts should disable stop actions or use Enhanced Mode
- MIG, Docker, MPS, Slurm, and ROCm details are documented in [docs/compatibility.md](docs/compatibility.md)

Useful docs:

- [Quickstart](docs/quickstart.md)
- [Security](docs/security.md)
- [Compatibility](docs/compatibility.md)
- [Lab setup](docs/lab_setup.md)
- [Changelog](CHANGELOG.md)
