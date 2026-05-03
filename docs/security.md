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

## Assistant API Mode

The Assistant is read-only and copy-only. In local mode it does not call an
external model. In BYO API mode, the browser sends your API URL, model, and API
key to the local LabGPU backend for that chat request, and LabGPU sends a
redacted workspace summary to the configured OpenAI-compatible endpoint.

LabGPU does not save Assistant API keys to `~/.labgpu/config.toml`. If you
choose "Remember key in this browser", the key is stored in browser
`localStorage` on that machine. Keep `labgpu ui` bound to `127.0.0.1` for this
mode.

## AI Providers and Remote Sessions

LabGPU may read non-secret provider state from local tools such as CC Switch:
provider names, current selections, and local proxy ports. It should not read
or display provider secret payloads. LabGPU may also perform a loopback TCP
check against the configured proxy port to distinguish "configured" from
"actually listening."

The preferred remote AI CLI workflow is Proxy Tunnel mode:

```bash
ssh -R REMOTE_GATEWAY_PORT:127.0.0.1:LOCAL_GATEWAY_PORT ALIAS
```

The remote server sees a loopback endpoint such as
`http://127.0.0.1:REMOTE_GATEWAY_PORT`; the real API key stays on the laptop or
in the local provider tool. The remote endpoint points to a session-scoped
LabGPU gateway, not directly to CC Switch. Claude Code sessions export a
temporary `ANTHROPIC_API_KEY=labgpu-session-*` token, and Codex CLI sessions use
a temporary `OPENAI_API_KEY=labgpu-session-*` token. The gateway validates the
token before forwarding to CC Switch, strips it before it reaches the local
proxy, and LabGPU does not copy API keys into the remote home directory for this
workflow. For Claude Code, LabGPU may create a mode-700 temporary directory
under `/tmp/labgpu-ai-*` containing a per-session `--settings` file and wrapper
script so `claude` uses the tunnel base URL. For Codex CLI, LabGPU creates a
temporary `CODEX_HOME` and wrapper under the same `/tmp/labgpu-ai-*` directory
with only `auth.json` and `config.toml` for the session gateway. These temporary
files contain only the session token, not the real provider key, and LabGPU does
not write remote `~/.codex` in Proxy Tunnel mode.

The session token is not a provider key, but it is still a temporary capability
token while the gateway is alive. On shared Linux accounts, other processes
running as the same Unix user may be able to inspect shell environments,
`/proc`, tmux panes, or shell startup state. Proxy Tunnel mode reduces secret
exposure, but it should not be treated as strong isolation on shared accounts.
LabGPU gateways close automatically after an idle timeout or hard lifetime, and
failed terminal launches close their gateway immediately.

Remote Config Override mode is an advanced personal-server workflow for Claude
Code and Codex CLI. LabGPU first backs up the existing remote config under
`~/.labgpu/ai-config-backups/...`, then writes remote `~/.claude/settings.json`
or `~/.codex/{auth.json,config.toml}` so the app points at the current LabGPU
session gateway. These files contain only the temporary `labgpu-session-*`
token and `127.0.0.1:<remote_port>` base URL, not the real provider key. A
restore script is written into the backup directory. Do not use this mode on
shared Linux accounts unless the user accepts that the temporary session token
will be stored in that remote home directory until restored or removed.

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
