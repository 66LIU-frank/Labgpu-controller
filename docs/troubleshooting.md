# Troubleshooting

## `nvidia-smi not found`

LabGPU can still run tests and demos with `labgpu status --fake`, but real GPU status requires NVIDIA drivers and `nvidia-smi`.

## `tmux not found`

`labgpu run` uses tmux so experiments survive SSH disconnects. Install tmux on the server before using `labgpu run`.

On Ubuntu/Debian:

```bash
sudo apt install tmux
```

## Permission denied while inspecting a PID

Some systems restrict cross-user process details. LabGPU should still show the PID and GPU memory when `nvidia-smi` exposes them, and fields such as cwd or command may be unavailable.

## Stale Running Runs

If a run still shows `running` after a server reboot, wrapper crash, or manual tmux deletion:

```bash
labgpu refresh
labgpu list --all
```

`refresh` marks runs as `orphaned` when neither the recorded PID nor tmux session exists.

## Ambiguous Kill Target

`labgpu kill NAME` refuses to act if multiple runs match `NAME`. Use the full run id:

```bash
labgpu kill exp-20260428-120000-ab12cd
```

## Web dashboard security

The MVP has no authentication. The default bind address is `127.0.0.1`; prefer SSH tunneling instead of exposing the port publicly.

## LABGPU_HOME Is Not Writable

Set a writable location:

```bash
export LABGPU_HOME=$HOME/.labgpu
```

For shared lab mode, ask an admin to create a writable shared directory with appropriate group permissions.

## `ssh alpha_liu` Fails on the Remote Server

SSH aliases such as `alpha_liu` live in your laptop's `~/.ssh/config`. After you log into `alpha_liu`, that remote server usually does not know the alias.

Run remote commands from your laptop:

```bash
ssh alpha_liu 'cd /tmp/labgpu && PYTHONPATH=src python3 -m labgpu status'
```

Or, once already logged into the remote server:

```bash
cd /tmp/labgpu
PYTHONPATH=src python3 -m labgpu status
```

For a local overview of many SSH-configured servers, use:

```bash
labgpu ui --hosts alpha_liu,Song-1
```

Run this from your laptop, not from inside the remote server shell. SSH aliases such as `alpha_liu` are defined in your laptop's `~/.ssh/config`; the remote server usually does not know them.

For debugging SSH discovery:

```bash
labgpu servers list
labgpu servers probe alpha_liu
```

## Stop buttons are missing

LabGPU Home only shows Stop buttons for GPU processes owned by the current SSH user. Actions are enabled by default on `127.0.0.1`; if you bind the UI to `0.0.0.0`, actions stay disabled unless you explicitly pass `--allow-actions`.
