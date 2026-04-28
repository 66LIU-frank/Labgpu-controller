# Quickstart

Use LabGPU as a personal GPU training workspace from your laptop and from SSH GPU servers.

## 1. Open Your Workspace

```bash
labgpu ui
```

For specific SSH aliases from your local `~/.ssh/config`:

```bash
labgpu ui --hosts alpha_liu,Song-1
```

The home page starts with Train Now / Recommended GPUs, then My Runs, Failed or Suspicious Runs, Problems, and Servers.

## 2. Pick a GPU From the Terminal

```bash
labgpu pick --min-vram 24G --prefer A100 --tag training
labgpu pick --min-vram 24G --prefer 4090 --cmd
```

`labgpu pick` ranks GPUs across SSH hosts. It is not a single-machine selector; it uses your SSH inventory.

## 3. Launch or Adopt Training

On the chosen GPU server:

```bash
labgpu run --name baseline --gpu 0 --config configs/base.yaml -- python train.py --config configs/base.yaml
```

For an already-running process:

```bash
labgpu adopt 23891 --name old_baseline --gpu 0 --log ./train.log
```

Each run gets a directory under `~/.labgpu/runs/`. The most important files are `meta.json`, `stdout.log`, `events.jsonl`, `command.sh`, `git.json`, `env.json`, and `diagnosis.json`.

## 4. Find Your Work Later

```bash
labgpu where
labgpu list
labgpu logs baseline --tail 100
```

## 5. Diagnose and Export Debug Context

```bash
labgpu diagnose baseline
labgpu context baseline --copy
labgpu report baseline
```

For demos or development machines without NVIDIA GPUs:

```bash
labgpu status --fake
labgpu demo
labgpu pick --fake-lab
labgpu where --fake-lab
```

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
