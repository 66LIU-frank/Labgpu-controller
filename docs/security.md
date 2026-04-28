# Security Notes

LabGPU is designed for student lab servers, where convenience matters but a
mistake can still interrupt another person's experiment. The default security
posture is conservative.

## Local-Only Web UI

`labgpu ui` and `labgpu web` bind to `127.0.0.1` by default. Keep that default
for daily use and reach remote machines through SSH from your own laptop.

If you bind to `0.0.0.0`, LabGPU disables mutating actions unless you explicitly
pass `--allow-actions`. The Alpha UI is not a replacement for a real
authenticated web service.

## Stop Own Process

Agentless stop actions are only shown for processes owned by the current SSH
user. Before sending a signal, LabGPU re-probes the remote server and verifies:

- PID
- process owner
- process start time
- command hash

This reduces PID-reuse mistakes. The default action sends `SIGTERM`; `SIGKILL`
is only used after an explicit force action.

For raw agentless processes, LabGPU stops the selected PID only. Child
processes may continue unless the process is part of a LabGPU-tracked run where
the local run manager can apply richer run semantics.

All stop attempts are written to:

```text
~/.labgpu/audit/actions.jsonl
```

## Shared Linux Accounts

If several people use the same Linux account, Agentless Mode cannot reliably
know which process belongs to which human. Mark that server as shared:

```toml
[servers.alpha_liu]
shared_account = true
allow_stop_own_process = false
```

In this mode, the UI should not expose Stop buttons for raw agentless
processes. Prefer Enhanced Mode with LabGPU-tracked runs if the lab wants safe
per-person actions.

## Command Redaction

LabGPU redacts sensitive-looking command arguments by default. Terms such as
`TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `PASSWD`, `OPENAI_API_KEY`,
`WANDB_API_KEY`, `HF_TOKEN`, `GITHUB_TOKEN`, and `AWS_SECRET_ACCESS_KEY` are
hidden before commands are rendered in the UI or copied into context snippets.

The UI does not display full environment variables by default. `labgpu context`
uses a safe environment subset unless `--include-env` is explicitly requested,
and even then redaction remains on unless `--no-redact` is used.

## Shared LABGPU_HOME

For a lab-wide run registry, prefer a group-owned directory:

```bash
sudo groupadd labgpu
sudo usermod -aG labgpu alice
sudo usermod -aG labgpu bob
sudo mkdir -p /shared/labgpu
sudo chgrp labgpu /shared/labgpu
sudo chmod 2770 /shared/labgpu
```

Then users can set:

```bash
export LABGPU_HOME=/shared/labgpu
```

Do not use a world-writable `chmod 1777 /shared/labgpu` setup for real lab
metadata. It is convenient, but it makes privacy and accidental overwrites much
harder to reason about.

## Personal-First Boundaries

For other users' processes, LabGPU favors non-destructive workflows:

- copy process info
- copy a polite owner message with server/GPU/PID/runtime/memory
- show possible-idle evidence without claiming certainty

LabGPU intentionally does not provide scheduler, reservation, quota, admin
panel, or kill-other-users features. The default UI is a personal workspace, not
a shared management dashboard.
