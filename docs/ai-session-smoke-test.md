# AI Session Smoke Test

Use this checklist before expanding the AI provider feature beyond Claude Code
Proxy Tunnel mode. Fake-lab mode validates pages and tests, but it does not
prove that a real SSH reverse tunnel works.

## Scope

This smoke test covers:

- non fake-lab LabGPU UI
- the Enter Server button
- Claude Code only
- Proxy Tunnel mode only
- current CC Switch Claude provider
- per-session LabGPU AI gateway
- optional remote working directory selected from VS Code Remote-SSH recents
- no vault, no Remote Write, no copied API keys

## Security Model

- LabGPU does not read or copy provider secrets.
- Remote servers receive only a temporary `labgpu-session-*` access token.
- Remote Claude Code talks to a LabGPU gateway through an SSH reverse tunnel.
- The gateway validates the session token before forwarding to CC Switch.
- The gateway is bound to `127.0.0.1` on the laptop.
- The gateway strips the session token before forwarding to CC Switch.
- Streaming responses such as `text/event-stream` are forwarded incrementally.
- Gateways have an idle timeout and a hard lifetime so abandoned sessions do
  not keep a local port open forever.
- Remote Write remains disabled in Alpha.

The session token is a temporary capability token, not a real provider key. On
shared Linux accounts, other processes running as the same Unix user may still
be able to inspect environment variables or tmux/shell state. Proxy Tunnel mode
keeps real provider secrets local, but it is not strong account isolation.

## Prerequisites

1. CC Switch is installed and has a current Claude provider.
2. The CC Switch Claude proxy is enabled and listening on loopback, usually:

   ```bash
   nc -vz 127.0.0.1 15721
   ```

3. The target SSH alias works from the laptop:

   ```bash
   ssh ALIAS
   ```

4. Claude Code is installed on the remote server and available as `claude` or
   `claude-code`.

## Start LabGPU

Run the real local UI, not fake-lab mode:

```bash
labgpu ui
```

Keep the UI bound to `127.0.0.1` unless you have a separate authenticated
deployment.

## Verify Provider State

1. Open `/providers`.
2. Confirm the Claude Code card shows the expected current provider.
3. Confirm the local proxy shows a loopback host and port.
4. Confirm the TCP check is `listening`.

The page must not show API keys, bearer tokens, provider secret JSON, or remote
write controls.

## Open Enter Server

1. Open `/gpus` or a server page.
2. Pick a real SSH server.
3. Click `Enter Server`.
4. Select `Claude Code`.
5. Select `Proxy Tunnel`.
6. Optionally choose a working directory imported from VS Code Remote-SSH
   recents, or type an absolute remote path such as `/data/lsg/work/OPSD`.
7. Confirm Remote Write, Codex, and Gemini are disabled.
8. Click `Open Terminal`.

The SSH command should include these properties:

```text
-tt
-o ExitOnForwardFailure=yes
-R 127.0.0.1:<remote_gateway_port>:127.0.0.1:<local_gateway_port>
ANTHROPIC_BASE_URL=http://127.0.0.1:<remote_gateway_port>
ANTHROPIC_API_KEY=labgpu-session-...
LABGPU_REMOTE_CWD=<selected folder, if any>
LABGPU_CLAUDE_SETTINGS=/tmp/labgpu-ai-.../claude-settings.json
```

It must not include a real provider key. The session token should not appear in
LabGPU UI history or logs.

When LabGPU creates its own reverse tunnel, it resolves the SSH alias and uses
an isolated SSH argv so `LocalForward` or `RemoteForward` entries from the
user's SSH config do not break the AI session tunnel.

## Verify Remote Shell

In the remote shell opened by LabGPU, run:

```bash
echo "$LABGPU_AI_MODE"
echo "$LABGPU_AI_APP"
echo "$LABGPU_AI_PROVIDER"
echo "$ANTHROPIC_BASE_URL"
echo "$ANTHROPIC_API_KEY"
echo "$CUDA_VISIBLE_DEVICES"
echo "$LABGPU_REMOTE_CWD"
echo "$LABGPU_CLAUDE_SETTINGS"
echo "$LABGPU_REAL_CLAUDE"
command -v claude
pwd
```

Expected values:

```text
proxy_tunnel
claude
<current CC Switch Claude provider>
http://127.0.0.1:<remote_gateway_port>
labgpu-session-...
<selected GPU index, or empty>
<selected folder, if any>
</tmp LabGPU Claude settings file>
<real Claude Code binary, for example /home/lsg/miniconda3/bin/claude>
<LabGPU wrapper path under /tmp/labgpu-ai-*>
<selected folder, if any>
```

LabGPU prepends common AI CLI paths such as `~/miniconda3/bin` to the launch
PATH, then creates a per-session Claude wrapper in `/tmp/labgpu-ai-*`. The
wrapper calls the real Claude Code binary with a temporary `--settings` file so
Claude Code uses the tunnel base URL without writing to `~/.claude`.

## Run AI Tunnel Doctor

The remote shell includes a temporary `aiswitch` helper in the same LabGPU
session directory as the Claude wrapper. It is read-only in this MVP: it can
show status and diagnose the tunnel, but it cannot switch providers.

Run:

```bash
aiswitch status
```

Expected output includes:

```text
LabGPU AI Session
Mode: proxy_tunnel
App: claude
Provider: <current CC Switch Claude provider>
Base URL: http://127.0.0.1:<remote_gateway_port>
Token: present (redacted)
```

Then run:

```bash
aiswitch doctor
```

Expected checks:

```text
No-token gateway check: 401
Authenticated session check: 200
Gateway session: ok
```

`aiswitch doctor` calls the gateway health endpoint with the session token, but
it must not print the full token or any real provider key.

Check that the remote loopback tunnel is reachable:

```bash
nc -vz 127.0.0.1 <remote_gateway_port>
```

Check that the gateway rejects callers without the session token:

```bash
curl -i "$ANTHROPIC_BASE_URL/v1/messages"
curl -i -H 'x-api-key: wrong' "$ANTHROPIC_BASE_URL/v1/messages"
```

Both should return:

```text
401 Unauthorized
```

Then start Claude Code:

```bash
claude
```

If Claude Code has a stable non-interactive command in your environment, run a
minimal request through that command instead.

During a streaming Claude response, confirm that output appears incrementally
rather than only after the whole response completes.

## Expected Failure Modes

If the local proxy is configured but not listening, LabGPU should report:

```text
CC Switch proxy is configured but not listening on 127.0.0.1:<port>.
```

If SSH exits with remote forwarding failure, the remote gateway port is probably
already in use on that server. Close the previous AI session or use another
remote gateway port when that option is available.

If Claude provider state is missing, LabGPU should report:

```text
Current CC Switch Claude provider was not found. Switch Claude provider in CC Switch first.
```

If Claude Code exists on the server but is not visible to the LabGPU launch
environment, report:

```text
claude not found in LabGPU launch PATH.
```

## Safety Checks

During the smoke test, confirm that:

- no real API key appears in the UI
- no real API key appears in the SSH command
- no Remote Write path is enabled
- `-R` binds the remote side to `127.0.0.1`
- `ExitOnForwardFailure=yes` is present
- the remote token starts with `labgpu-session-`, not `sk-`
- unauthenticated remote gateway requests return `401`
- Claude Code streaming output is incremental
- Recent AI Sessions does not claim that a tunnel is online
