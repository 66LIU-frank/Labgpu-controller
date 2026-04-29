# LabGPU

[![CI](https://github.com/66LIU-frank/Labgpu-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/66LIU-frank/Labgpu-controller/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-v0.1.0--alpha-orange)

[English](README.md) | [简体中文](README.zh-CN.md)

Personal GPU workspace for students using shared SSH servers.

Find a free GPU. Launch training. Track your runs. Diagnose failures.
No daemon. No root. No Slurm. No Kubernetes.

![LabGPU Home demo preview](docs/assets/labgpu-home-live.png)

```text
find GPU -> run/adopt -> observe -> diagnose -> context/report -> safe action
```

## Get Started in 3 Minutes

Basic mode runs on your laptop. It reads your `~/.ssh/config`, probes selected SSH hosts, and opens a local workspace. You do not need root access, a remote daemon, Slurm, Kubernetes, or a shared tracking server.

Try the fake multi-server demo first:

```bash
pipx install git+https://github.com/66LIU-frank/Labgpu-controller.git
labgpu demo
labgpu pick --fake-lab
```

Use real SSH GPU servers in three steps:

```bash
labgpu init --hosts alpha_liu,alpha_shi --tags A100,training
labgpu ui
labgpu pick --min-vram 24G --prefer A100
```

Then copy the SSH/CUDA command from `Train Now`, or run LabGPU on the chosen GPU server to save a full run capsule:

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
| Move a project | `labgpu sync` streams a project from one SSH server to another through your laptop. |
| Check transfer speed | `labgpu nettest` measures effective copy speed before you move a project. |
| Recover experiment context | Run capsules save command, log, git, config, env summary, and GPU info. |
| Debug failures | `diagnose` and Failure Inbox catch OOM, traceback, NCCL, disk full, killed, NaN, and suspected idle. |
| Ask AI or teammates for help | `labgpu context --copy` exports one redacted Markdown debug context. |
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

Move a project to another GPU server:

```bash
labgpu nettest alpha_liu alpha_shi --mb 64
labgpu sync alpha_liu:/data/me/project alpha_shi:/data/me/project
labgpu sync alpha_liu:/data/me/project alpha_shi:/data/me/project --execute --yes
```

`sync` streams through your laptop by default, so it does not require the two servers to SSH into each other. Add `--direct` to `nettest` only when the source server can SSH into the target server.

## UI Layout

LabGPU Home is training-first:

```text
Train Now
  Recommended GPUs ranked by GPU availability, free VRAM, model, load, and tags.
  Each card can copy commands or open an SSH terminal for that server.

My Runs
  LabGPU runs, adopted runs, and own untracked GPU processes.

Failed or Suspicious Runs
  OOM, traceback, NCCL, disk full, killed, NaN, suspected idle, and stale logs.

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
labgpu pick [--min-vram 24G] [--prefer A100] [--tag training] [--explain] [--cmd "COMMAND"] [--json]
labgpu where [--json]
labgpu nettest SRC_HOST DST_HOST [--mb 64] [--both] [--direct] [--json]
labgpu sync SRC_HOST:/project DST_HOST:/project [--execute] [--exclude PATTERN]

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

## Future TODO: LabGPU Agent

LabGPU's core is the personal GPU training workflow above. The AI assistant is intentionally not the main promise yet. The longer-term direction is:

- ask in natural language: "find me a 24GB+ A100 and prepare a launch command"
- generate safe, copyable launch/adopt/rerun plans from current GPU and run state
- explain failed runs from logs, configs, git state, env summary, and GPU history
- produce one redacted debug context for ChatGPT, Claude, Codex, or a teammate
- optionally run approved LabGPU actions later, but never expose arbitrary SSH shell execution

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
