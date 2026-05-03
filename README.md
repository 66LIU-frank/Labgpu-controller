# LabGPU

[![CI](https://github.com/66LIU-frank/Labgpu-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/66LIU-frank/Labgpu-controller/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-v0.1.1--alpha-orange)

[English](README.md) | [简体中文](README.zh-CN.md)

LabGPU is a local-first GPU workspace for students using shared SSH servers.
It helps you find usable GPUs, enter the right server and folder, track your own
training, diagnose failures, and run remote AI CLIs without copying provider
keys to the server.

No root. No remote daemon. No Slurm or Kubernetes requirement. No shared
tracking server.

<p align="center">
  <img src="docs/assets/labgpu-hero.png" alt="LabGPU promotional hero illustration" width="100%">
</p>

<p align="center">
  <img src="docs/assets/labgpu-home-live.png" alt="LabGPU real UI example" width="100%">
</p>

```text
find GPU -> enter server -> run/adopt -> observe -> diagnose -> context/report
```

## Why LabGPU

Most lab GPU users already have SSH access but no clean personal workspace.
LabGPU keeps that model:

- it runs on your laptop
- it reads your normal `~/.ssh/config`
- it probes servers through SSH
- it does not install a daemon on shared machines
- it keeps dangerous actions conservative and personal-first
- it keeps AI provider secrets local by default

## Main UI

The UI is intentionally grouped into five primary areas:

| Area | What you use it for |
| --- | --- |
| Home | Overview, recommended GPUs, current work, problems, saved servers. |
| Train | Find GPUs, open terminals, see `My Runs` and `My GPU Processes`. |
| Servers | Inspect SSH servers, disks, GPUs, health, and group shortcuts. |
| AI Config | CC Switch-style provider routing, compact app status, and remote Claude Code / Codex CLI session defaults. |
| Settings | Add/import SSH servers, choose saved servers, and manage groups. |

Secondary tools still exist, but they stay inside related workflows:

- `Groups` is reachable from Home, Servers, and Settings.
- `Problems` is reachable from Home and Servers.
- `Assistant` is reachable from Home.
- raw JSON links are hidden unless enabled in Settings.

## AI Config Console

LabGPU includes an AI Config Console for CC Switch-style provider routing and
remote session setup. It connects remote Claude Code and Codex CLI sessions to
your local CC Switch provider without writing API keys to the remote server.

The current supported path is:

```text
Enter Server
  -> Claude Code or Codex CLI
  -> Proxy Tunnel
  -> local LabGPU session gateway
  -> selected CC Switch provider through the local LabGPU gateway
  -> current provider
```

What works now:

- read non-secret CC Switch provider state
- switch existing CC Switch providers from LabGPU by updating CC Switch's
  effective current-provider state
- open a remote shell in a selected working directory
- create an SSH reverse tunnel with a per-session gateway
- inject a temporary Claude Code wrapper/settings file under remote `/tmp`
- inject a temporary Codex `CODEX_HOME` and wrapper under remote `/tmp` (beta)
- use the selected CC Switch Claude/Codex provider config locally inside the
  gateway, without copying real provider keys to the remote server
- detect common remote AI CLI locations and allow per-server command overrides
- optionally back up and overwrite remote Claude/Codex config for the current
  gateway session, without copying real provider keys
- optionally create a separate Network Tunnel after SSH connects, forwarding a
  laptop proxy port to the remote shell as `HTTP_PROXY` / `HTTPS_PROXY` /
  `ALL_PROXY`
- run read-only `aiswitch status` / `aiswitch doctor` inside the remote shell
- keep real provider keys on the laptop or in CC Switch

What is intentionally not built into LabGPU yet:

- adding new providers with API keys
- writing provider keys into remote `~/.claude`, `~/.codex`, or `~/.gemini`
- multi-user provider vaults
- Gemini/OpenClaw remote AI sessions

Add new providers in CC Switch for now. LabGPU will show them after refresh and
can switch among existing providers without reading their secrets. Switching is
limited to CC Switch's current-provider settings and database flag. For
Claude/Codex Proxy Tunnel, LabGPU may read the selected CC Switch provider key locally
inside the session gateway so remote servers still receive only a
`labgpu-session-*` token. LabGPU does not create or store provider keys.

## Quick Start

Download a desktop build:

- macOS: download `LabGPU-<version>-macOS.dmg` from
  [GitHub Releases](https://github.com/66LIU-frank/Labgpu-controller/releases)
- Windows: download `LabGPU-<version>-Windows.zip` from
  [GitHub Releases](https://github.com/66LIU-frank/Labgpu-controller/releases)

If Releases is empty or behind `main`, push a fresh version tag. GitHub Actions
only publishes release artifacts for commits that receive a `v*` tag.

Install from GitHub:

```bash
pipx install git+https://github.com/66LIU-frank/Labgpu-controller.git
```

No `pipx`:

```bash
curl -fsSL https://raw.githubusercontent.com/66LIU-frank/Labgpu-controller/main/install.sh | sh
```

Open the local UI:

```bash
labgpu ui
```

Open an app-style desktop window:

```bash
labgpu desktop
```

Create a macOS app wrapper:

```bash
labgpu desktop --install-app
open ~/Applications/LabGPU.app
```

Build release launcher packages:

```bash
scripts/package_macos_dmg.sh
scripts/package_windows_zip.sh
```

These packages are lightweight launchers/installers. They install or call the
normal `labgpu` command, then start the same local-first UI.

Build standalone desktop packages:

```bash
python scripts/build_standalone.py --clean
```

The standalone path uses PyInstaller. On GitHub, the `Release Build` workflow
builds macOS `.dmg` and Windows `.zip` artifacts. Manual runs upload artifacts
to the Actions run; tag pushes also publish a GitHub Release with those
artifacts attached.

```bash
git tag -a v0.1.1-alpha -m "LabGPU v0.1.1 alpha"
git push origin v0.1.1-alpha
```

For a fixed server set:

```bash
labgpu ui --hosts alpha_liu,alpha_shi
```

Before using real servers, make sure:

- Python 3.10+ is available on your laptop
- `ssh YOUR_ALIAS` works, or you know the host/user/key details
- NVIDIA GPU servers have `nvidia-smi`

## Add Servers

If your aliases already exist in `~/.ssh/config`:

```bash
labgpu init --hosts alpha_liu,alpha_shi --tags A100,training
```

Or use `Settings` in the UI to:

- add a new SSH server
- import existing SSH aliases
- optionally append a safe `Host` block to `~/.ssh/config`
- choose which servers appear by default
- create server groups such as `AlphaLab`, `off-campus`, or `H800`

LabGPU does not create SSH keys. Password login, SSH keys, ssh-agent,
`IdentityFile`, and `ProxyJump` stay in normal SSH config.

## Find and Enter a GPU Server

Use the Train page or CLI:

```bash
labgpu pick --min-vram 24G --prefer A100 --explain
labgpu pick --min-vram 24G --prefer 4090 --cmd "python train.py --config configs/sft.yaml"
```

Each GPU card can copy:

- `ssh HOST`
- `CUDA_VISIBLE_DEVICES=GPU_INDEX`
- a launch snippet
- an Enter Server terminal action

Enter Server can also open the remote shell in a recent VS Code Remote-SSH
folder, route Claude Code or Codex CLI through the local provider tunnel, and
optionally expose your laptop proxy to the remote shell through an SSH
RemoteForward. This Network Tunnel does not proxy SSH itself; it only helps
commands after login reach the network through your local proxy.

## Track Your Training

Start a tracked run:

```bash
labgpu run --name sft --gpu auto --min-vram 24G -- python train.py --config configs/sft.yaml
```

Adopt an already-running process:

```bash
labgpu adopt 23891 --name old_baseline --log ./train.log
```

Find your work later:

```bash
labgpu where
labgpu list
labgpu logs sft --tail 100
```

Each tracked or adopted run gets a plain-file capsule under
`~/.labgpu/runs/`:

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

## Diagnose and Share Context

```bash
labgpu diagnose sft
labgpu context sft --copy
labgpu report sft
```

LabGPU looks for common signals such as OOM, traceback, NCCL errors, disk full,
killed processes, NaN, stale logs, suspected idle GPU memory, and zombie/IO-wait
states.

`labgpu context --copy` creates a redacted Markdown context you can send to an
assistant or teammate.

## Move Projects

```bash
labgpu nettest alpha_liu alpha_shi --mb 64
labgpu sync alpha_liu:/data/me/project alpha_shi:/data/me/project
labgpu sync alpha_liu:/data/me/project alpha_shi:/data/me/project --execute --yes
```

By default `sync` streams through your laptop, so the two servers do not need to
SSH into each other.

## Modes

**Agentless SSH Mode** is the default. LabGPU runs locally and probes servers
with standard remote tools such as `nvidia-smi`, `ps`, `df`, `free`, and
`uptime`.

**Enhanced Mode** is optional. If the remote PATH has `labgpu`, the UI can also
read remote run metadata with:

```bash
labgpu status --json
labgpu list --json
```

Failure to enter Enhanced Mode never breaks Agentless Mode.

## Architecture

LabGPU keeps the control plane local. SSH remains the server boundary, while AI
sessions route through a session-scoped local gateway before reaching the local
provider proxy.

<p align="center">
  <img src="docs/assets/labgpu-ai-workflow.svg" alt="LabGPU workflow and AI proxy tunnel architecture" width="100%">
</p>

## Safety

LabGPU is personal-first. It is not a scheduler, reservation system, quota
system, admin panel, Slurm/Kubernetes replacement, or a tool for managing other
people's jobs.

Safe stop actions:

- only show for processes owned by the current SSH user
- are disabled for shared Linux accounts unless configured otherwise
- re-probe PID/user/start time/command hash before acting
- send SIGTERM by default
- require explicit force for SIGKILL
- are disabled outside loopback unless `--allow-actions` is set

AI session safety:

- LabGPU reads provider names, current selections, and proxy ports for summary/switching
- real provider keys stay in CC Switch or local provider tooling; Claude/Codex
  Proxy Tunnel may use the selected CC Switch provider key inside the local gateway only
- remote servers receive only a temporary `labgpu-session-*` token
- the local gateway validates the token before forwarding
- Remote Config Override backs up remote Claude/Codex config and writes only the
  session gateway token/base URL, not real provider keys
- Network Tunnel forwards a local proxy port to the remote shell but does not
  copy proxy credentials or provider keys

See [docs/security.md](docs/security.md) for the full model.

## Commands

```text
labgpu init [--hosts alpha_liu,alpha_shi] [--tags A100,training]
labgpu ui [--hosts alpha_liu,alpha_shi] [--fake-lab]
labgpu desktop [--hosts alpha_liu,alpha_shi] [--fake-lab]
labgpu desktop --install-app [PATH]
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

## Documentation

- [Quickstart](docs/quickstart.md)
- [AI Session Smoke Test](docs/ai-session-smoke-test.md)
- [Security](docs/security.md)
- [Distribution](docs/distribution.md)
- [Compatibility](docs/compatibility.md)
- [Lab setup](docs/lab_setup.md)
- [Design](docs/design.md)
- [Roadmap and Feature Status](docs/roadmap.md)

## Status

LabGPU is alpha. It currently targets NVIDIA servers through SSH and
`nvidia-smi`, with local run capsules, GPU ranking, session-scoped AI proxy
tunnels, Failure Inbox, redacted context export, transfer helpers, and safe
own-process actions.
