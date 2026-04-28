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
labgpu servers --hosts alpha_liu,Song-1
```
