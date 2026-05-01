# Roadmap and Feature Status

This page keeps the current LabGPU scope explicit. It is meant to prevent
experimental UI from looking more complete than the implementation really is.

## Stable Enough for Daily Use

These features are implemented and covered by tests or real smoke tests:

- local UI grouped around Home, Train, Servers, AI Sessions, and Settings
- SSH host discovery from `~/.ssh/config`
- saved server inventory and server groups
- agentless GPU/server probing over SSH
- Train page GPU recommendations
- My Runs and own GPU process visibility
- Failure Inbox and Problems views
- safe own-process stop guardrails
- `labgpu run`, `adopt`, `where`, `logs`, `diagnose`, `context`, and `report`
- transfer helpers: `nettest` and `sync`
- Enter Server with working directory selection
- VS Code Remote-SSH recent folder import
- Claude Code through Proxy Tunnel
- session-scoped local AI gateway with token auth, streaming, and cleanup
- read-only remote `aiswitch status` / `aiswitch doctor`
- CC Switch non-secret provider status reading
- switching among existing CC Switch providers
  - switches local current-provider state only
  - does not read, store, or create provider API keys

## Alpha but Usable With Care

These are useful, but the behavior should stay conservative:

- Assistant page
  - local rule-based mode is read-only/copy-only
  - BYO API mode sends redacted context to the configured endpoint
  - no tool execution or automatic actions
- CC Switch provider switching
  - currently updates CC Switch provider state directly in the local database
  - switch results are verified after write and labeled as local state updates
  - safer long term path is an official CC Switch API/CLI if available
- browser-local Recent AI Sessions
  - launch history only
  - not a tunnel health monitor

## Not Implemented Yet

These should not be presented as working product features:

- adding new AI providers or API keys inside LabGPU
- local encrypted provider vault
- Remote Write into remote `~/.claude`, `~/.codex`, `~/.gemini`
- Codex remote AI session launcher
- Gemini remote AI session launcher
- OpenClaw remote AI session launcher
- remote `aiswitch use`
- multi-user/team dashboard
- authentication, RBAC, audit-grade team permissions
- scheduler, quota, reservation, or queue system

## Recommended Next PRs

1. Codex Session Design

   Do not simply copy the Claude path. First verify:

   - what Codex CLI expects for base URL and auth
   - whether CC Switch proxy supports Codex routing in the same mode
   - whether streaming and config reload behavior match Claude Code

2. CC Switch API/CLI Adapter

   Keep the current local database path as a fallback, but prefer an official
   CC Switch API/CLI when one is stable.

3. README and Screenshot Refresh

   Keep README top-of-page focused on:

   - one clean promotional image
   - one real UI screenshot
   - a short statement of what is implemented today

## Product Principle

LabGPU should remain a local-first personal workspace. The first product loop is:

```text
find GPU -> enter server -> run/adopt -> observe -> diagnose -> context/report
```

AI provider features should support that loop without turning LabGPU into a
secret manager or public admin dashboard.
