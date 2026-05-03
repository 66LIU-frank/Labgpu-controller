# AI Session Smoke Test

Use this checklist before expanding the AI provider feature beyond Claude Code
and Codex CLI Proxy Tunnel mode. Fake-lab mode validates pages and tests, but it
does not prove that a real SSH reverse tunnel works.

## Scope

This smoke test covers:

- non fake-lab LabGPU UI
- the Enter Server button
- Claude Code and Codex CLI
- Proxy Tunnel mode only
- current CC Switch provider for the selected app
- per-session LabGPU AI gateway
- optional remote working directory selected from VS Code Remote-SSH recents
- optional Network Tunnel for forwarding a laptop proxy port into the remote
  shell after SSH connects
- no vault, no copied real provider API keys

## Security Model

- LabGPU does not copy provider secrets to the remote server in Proxy Tunnel
  mode. Claude uses the local provider proxy. Codex may read the selected CC
  Switch Codex provider key locally inside the LabGPU gateway when direct
  provider forwarding is needed.
- Remote servers receive only a temporary `labgpu-session-*` access token.
- Remote Claude Code or Codex CLI talks to a LabGPU gateway through an SSH
  reverse tunnel.
- The gateway validates the session token before forwarding to CC Switch.
- The gateway is bound to `127.0.0.1` on the laptop.
- The gateway strips the session token before forwarding to CC Switch.
- Streaming responses such as `text/event-stream` are forwarded incrementally.
- Gateways have an idle timeout and a hard lifetime so abandoned sessions do
  not keep a local port open forever.
- Remote Config Override is advanced. It backs up and overwrites remote
  Claude/Codex config with the session gateway token/base URL, not real
  provider keys.
- Network Tunnel is separate from SSH connectivity. It does not make SSH itself
  use a proxy. After SSH connects, LabGPU can create an extra `ssh -R` so remote
  shell commands see your laptop proxy as `HTTP_PROXY`, `HTTPS_PROXY`, and
  `ALL_PROXY`.

The session token is a temporary capability token, not a real provider key. On
shared Linux accounts, other processes running as the same Unix user may still
be able to inspect environment variables or tmux/shell state. Proxy Tunnel mode
keeps real provider secrets local, but it is not strong account isolation.

## Prerequisites

1. CC Switch is installed and has a current provider for the selected app.
2. For Codex tests, CC Switch has a current Codex provider with API key and
   `base_url` in its provider config.
3. The selected CC Switch app proxy is enabled and listening on loopback,
   usually:

   ```bash
   nc -vz 127.0.0.1 15721
   ```

4. The target SSH alias works from the laptop:

   ```bash
   ssh ALIAS
   ```

5. For Claude tests, Claude Code is installed on the remote server and available
   as `claude` or `claude-code`.
6. For Codex tests, Codex CLI is installed on the remote server and available as
   `codex`.

## Start LabGPU

Run the real local UI, not fake-lab mode:

```bash
labgpu ui
```

Keep the UI bound to `127.0.0.1` unless you have a separate authenticated
deployment.

## Verify Provider State

1. Open `/providers`.
2. Confirm the Claude Code or Codex CLI card shows the expected current
   provider.
3. Confirm the local proxy shows a loopback host and port.
4. Confirm the TCP check is `listening`.

The page must not show API keys, bearer tokens, provider secret JSON, or remote
write controls.

## Open Enter Server

1. Open `/gpus` or a server page.
2. Pick a real SSH server.
3. Click `Enter Server`.
4. Select `Claude Code` or `Codex CLI`.
5. Select `Proxy Tunnel`.
6. Optionally choose a working directory imported from VS Code Remote-SSH
   recents, or type an absolute remote path such as `/data/lsg/work/OPSD`.
7. Optional: enable `Network Tunnel` and enter your laptop proxy port, for
   example `7890` or `33210`. Leave the remote proxy port empty unless you need
   a fixed port.
8. Confirm Remote Config Override is marked advanced, while Gemini and OpenClaw are disabled.
9. Click `Open Terminal`.

The SSH command should include these properties:

```text
-tt
-o ExitOnForwardFailure=yes
-R 127.0.0.1:<remote_gateway_port>:127.0.0.1:<local_gateway_port>
-R 127.0.0.1:<remote_network_proxy_port>:127.0.0.1:<local_proxy_port>  # Network Tunnel only
ANTHROPIC_BASE_URL=http://127.0.0.1:<remote_gateway_port>
ANTHROPIC_API_KEY=labgpu-session-...
OPENAI_BASE_URL=http://127.0.0.1:<remote_gateway_port>/v1  # Codex only
OPENAI_API_KEY=labgpu-session-...                          # Codex only
CODEX_HOME=/tmp/labgpu-ai-.../codex-home                   # Codex only
LABGPU_REMOTE_CWD=<selected folder, if any>
LABGPU_CLAUDE_SETTINGS=/tmp/labgpu-ai-.../claude-settings.json  # Claude only
HTTP_PROXY=http://127.0.0.1:<remote_network_proxy_port>     # Network Tunnel only
ALL_PROXY=http://127.0.0.1:<remote_network_proxy_port>      # Network Tunnel only
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
echo "$OPENAI_BASE_URL"
echo "$OPENAI_API_KEY"
echo "$CODEX_HOME"
echo "$CUDA_VISIBLE_DEVICES"
echo "$LABGPU_REMOTE_CWD"
echo "$LABGPU_CLAUDE_SETTINGS"
echo "$LABGPU_REAL_CLAUDE"
echo "$LABGPU_REAL_CODEX"
echo "$LABGPU_NETWORK_PROXY_URL"
echo "$HTTP_PROXY"
echo "$ALL_PROXY"
command -v claude
command -v codex
pwd
```

Expected values:

```text
proxy_tunnel
claude or codex
<current CC Switch provider for the selected app>
http://127.0.0.1:<remote_gateway_port>   # selected app base URL
labgpu-session-...                       # selected app token
<selected GPU index, or empty>
<selected folder, if any>
<selected app temp config path, if any>
<real app binary, for example /home/lsg/miniconda3/bin/claude or /usr/local/bin/codex>
<LabGPU wrapper path under /tmp/labgpu-ai-*>
<selected folder, if any>
```

LabGPU prepends common AI CLI paths such as `~/miniconda3/bin` to the launch
PATH, then creates a per-session app wrapper in `/tmp/labgpu-ai-*`. The Claude
wrapper calls the real Claude Code binary with a temporary `--settings` file so
Claude Code uses the tunnel base URL without writing to `~/.claude`. The Codex
wrapper calls the real Codex binary with a temporary `CODEX_HOME` containing
session-only `auth.json` and `config.toml`, without writing to `~/.codex`.
Remote Codex still receives only the session token; if direct provider
forwarding is active, the real selected provider key is used only by the local
LabGPU gateway on the laptop.

## Run AI Tunnel Doctor

The remote shell includes a temporary `aiswitch` helper in the same LabGPU
session directory as the selected app wrapper. It is read-only in this MVP: it can
show status and diagnose the tunnel, but it cannot switch providers.

Run:

```bash
aiswitch status
```

Expected output includes:

```text
LabGPU AI Session
Mode: proxy_tunnel
App: claude or codex
Provider: <current CC Switch provider>
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
BASE_URL="${ANTHROPIC_BASE_URL:-$OPENAI_BASE_URL}"
curl -i "$BASE_URL/v1/messages"
curl -i -H 'x-api-key: wrong' "$BASE_URL/v1/messages"
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

During a streaming Claude or Codex response, confirm that output appears
incrementally rather than only after the whole response completes.

For Codex CLI beta, first confirm the wrapper and temporary home:

```bash
command -v codex
echo "$CODEX_HOME"
test -f "$CODEX_HOME/auth.json"
test -f "$CODEX_HOME/config.toml"
```

Then run a minimal Codex command appropriate for your installed version, for
example `codex --help` for a no-cost wrapper check, or a small `codex exec`
request if you intentionally want to exercise the provider path. Confirm
streaming/output behavior in the same way as Claude.

## Expected Failure Modes

If the local proxy is configured but not listening, LabGPU should report:

```text
CC Switch proxy is configured but not listening on 127.0.0.1:<port>.
```

If SSH exits with remote forwarding failure, the remote gateway port is probably
already in use on that server. Close the previous AI session or use another
remote gateway port when that option is available.

If selected app provider state is missing, LabGPU should report:

```text
Current CC Switch <app> provider was not found. Switch <app> provider in AI Config Console or CC Switch first.
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
- Remote Config Override writes only a temporary session token/base URL and creates a restore script
- `-R` binds the remote side to `127.0.0.1`
- `ExitOnForwardFailure=yes` is present
- the remote token starts with `labgpu-session-`, not `sk-`
- unauthenticated remote gateway requests return `401`
- Claude Code streaming output is incremental
