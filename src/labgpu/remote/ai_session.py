from __future__ import annotations

import re
import json
import shlex
from dataclasses import dataclass


SUPPORTED_AI_APPS = {"claude", "codex"}
AI_APP_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex CLI",
}
SUPPORTED_MODES = {"proxy_tunnel", "remote_write"}
DUMMY_PROXY_API_KEY = "labgpu-proxy"
SESSION_TOKEN_PREFIX = "labgpu-session-"
SAFE_SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
SAFE_GPU_INDEX_RE = re.compile(r"^\d+(,\d+)*$")
SAFE_SESSION_TOKEN_RE = re.compile(r"^labgpu-session-[A-Za-z0-9_-]{24,}$")
DEFAULT_AI_PATH_PREFIXES = ("~/miniconda3/bin", "~/.local/bin")


@dataclass(frozen=True)
class EnterServerAIRequest:
    server_alias: str
    gpu_index: str | None
    ai_app: str
    provider_name: str
    ccswitch_proxy_port: int
    local_gateway_port: int
    remote_gateway_port: int
    session_token: str
    mode: str = "proxy_tunnel"
    remote_cwd: str | None = None
    ssh_options: tuple[str, ...] = ()
    ssh_target: str | None = None
    remote_path_prefixes: tuple[str, ...] = DEFAULT_AI_PATH_PREFIXES
    claude_command: str | None = None
    codex_command: str | None = None


@dataclass(frozen=True)
class EnterServerAICommand:
    ssh_args: list[str]
    remote_env: dict[str, str]
    display_summary: str
    token_fingerprint: str


def build_ai_ssh_command(request: EnterServerAIRequest) -> EnterServerAICommand:
    validate_request(request)
    remote_url = f"http://127.0.0.1:{request.remote_gateway_port}"
    remote_cwd = normalized_remote_cwd(request.remote_cwd)
    path_prefixes = normalized_remote_path_prefixes(request.remote_path_prefixes)
    claude_command = normalized_remote_command_path(request.claude_command)
    codex_command = normalized_remote_command_path(request.codex_command)
    remote_env = {
        "LABGPU_AI_MODE": request.mode,
        "LABGPU_AI_APP": request.ai_app,
        "LABGPU_AI_PROVIDER": request.provider_name,
        "LABGPU_AI_BASE_URL": remote_url,
        "LABGPU_AI_SESSION_TOKEN": request.session_token,
    }
    if request.ai_app == "claude":
        remote_env.update(
            {
                "ANTHROPIC_BASE_URL": remote_url,
                "ANTHROPIC_API_KEY": request.session_token,
            }
        )
    elif request.ai_app == "codex":
        remote_env.update(
            {
                "OPENAI_BASE_URL": remote_url,
                "OPENAI_API_KEY": request.session_token,
            }
        )
    if remote_cwd is not None:
        remote_env["LABGPU_REMOTE_CWD"] = remote_cwd
    if path_prefixes:
        remote_env["LABGPU_AI_PATH_PREFIX"] = ":".join(path_prefixes)
    if claude_command is not None:
        remote_env["LABGPU_AI_CLAUDE_COMMAND"] = claude_command
    if codex_command is not None:
        remote_env["LABGPU_AI_CODEX_COMMAND"] = codex_command
    gpu = normalized_gpu_index(request.gpu_index)
    if gpu is not None:
        remote_env["CUDA_VISIBLE_DEVICES"] = gpu
    remote_command = build_remote_shell_command(
        remote_env,
        remote_cwd=remote_cwd,
        remote_path_prefixes=path_prefixes,
        setup_ai_session=True,
    )
    tunnel = f"127.0.0.1:{request.remote_gateway_port}:127.0.0.1:{request.local_gateway_port}"
    return EnterServerAICommand(
        ssh_args=[
            "ssh",
            "-tt",
            "-o",
            "ExitOnForwardFailure=yes",
            *request.ssh_options,
            "-R",
            tunnel,
            request.ssh_target or request.server_alias,
            remote_command,
        ],
        remote_env=remote_env,
        display_summary=(
            f"{request.server_alias} / {ai_app_label(request.ai_app)} / {request.provider_name} / "
            f"Proxy Tunnel remote 127.0.0.1:{request.remote_gateway_port} -> "
            f"local gateway 127.0.0.1:{request.local_gateway_port} -> CC Switch 127.0.0.1:{request.ccswitch_proxy_port}"
        ),
        token_fingerprint=request.session_token[-8:],
    )


def build_remote_shell_command(
    remote_env: dict[str, str],
    *,
    remote_cwd: str | None = None,
    remote_path_prefixes: tuple[str, ...] | list[str] = (),
    setup_ai_session: bool = False,
) -> str:
    exports = [f"export {key}={shlex.quote(value)}" for key, value in remote_env.items()]
    path_export = build_path_export(remote_path_prefixes)
    if path_export:
        exports.append(path_export)
    if setup_ai_session:
        exports.append(build_ai_session_setup(remote_env))
    if remote_cwd is not None:
        exports.append(f"cd {shlex.quote(remote_cwd)} || exit 1")
    exports.append(build_interactive_shell_exec(setup_ai_session=setup_ai_session))
    return "; ".join(exports)


def build_ai_session_setup(remote_env: dict[str, str]) -> str:
    aiswitch = build_aiswitch_helper()
    bashrc = build_ai_shell_rc(".bashrc")
    zshrc = build_ai_shell_rc(".zshrc")
    parts = [
        'LABGPU_AI_TMPDIR="${TMPDIR:-/tmp}/labgpu-ai-${USER:-user}-$$"',
        "&&",
        'mkdir -p "$LABGPU_AI_TMPDIR"',
        "&&",
        'chmod 700 "$LABGPU_AI_TMPDIR"',
        "&&",
        'export LABGPU_AI_TMPDIR',
        "&&",
        f"printf %s {shlex.quote(aiswitch)} > \"$LABGPU_AI_TMPDIR/aiswitch\"",
        "&&",
        'chmod 700 "$LABGPU_AI_TMPDIR/aiswitch"',
        "&&",
        f"printf %s {shlex.quote(bashrc)} > \"$LABGPU_AI_TMPDIR/bashrc\"",
        "&&",
        f"printf %s {shlex.quote(zshrc)} > \"$LABGPU_AI_TMPDIR/.zshrc\"",
        "&&",
        'export PATH="$LABGPU_AI_TMPDIR:$PATH"',
    ]
    app = remote_env.get("LABGPU_AI_APP")
    if app == "claude":
        parts.extend([";", build_claude_app_setup(remote_env)])
    elif app == "codex":
        parts.extend([";", build_codex_app_setup(remote_env)])
    if remote_env.get("LABGPU_AI_MODE") == "remote_write":
        parts.extend([";", build_remote_config_override_setup(remote_env)])
    return " ".join(parts)


def build_ai_shell_rc(user_rc: str) -> str:
    return (
        f'if [ -r "$HOME/{user_rc}" ]; then . "$HOME/{user_rc}"; fi\n'
        'export PATH="$LABGPU_AI_TMPDIR:$PATH"\n'
        'if [ -n "$LABGPU_REMOTE_CWD" ]; then cd "$LABGPU_REMOTE_CWD" 2>/dev/null || true; fi\n'
    )


def build_claude_app_setup(remote_env: dict[str, str]) -> str:
    remote_write = remote_env.get("LABGPU_AI_MODE") == "remote_write"
    settings = json.dumps(
        {
            "env": {
                "ANTHROPIC_BASE_URL": remote_env["ANTHROPIC_BASE_URL"],
                "ANTHROPIC_API_KEY": remote_env["ANTHROPIC_API_KEY"],
            }
        },
        separators=(",", ":"),
    )
    wrapper = '#!/bin/sh\nexec "$LABGPU_REAL_CLAUDE" "$@"\n' if remote_write else '#!/bin/sh\nexec "$LABGPU_REAL_CLAUDE" --settings "$LABGPU_CLAUDE_SETTINGS" "$@"\n'
    parts = [
        'LABGPU_REAL_CLAUDE="${LABGPU_AI_CLAUDE_COMMAND:-}"',
        "&&",
        'if [ -z "$LABGPU_REAL_CLAUDE" ]; then LABGPU_REAL_CLAUDE="$(command -v claude || command -v claude-code || true)"; fi',
        ";",
        'case "$LABGPU_REAL_CLAUDE" in "~/"*) LABGPU_REAL_CLAUDE="${HOME}/${LABGPU_REAL_CLAUDE#~/}" ;; esac',
        ";",
        'export LABGPU_REAL_CLAUDE',
        ";",
        'if [ -n "$LABGPU_REAL_CLAUDE" ]; then',
    ]
    if remote_write:
        parts.extend(
            [
                f"printf %s {shlex.quote(wrapper)} > \"$LABGPU_AI_TMPDIR/claude\"",
                "&&",
                'chmod 700 "$LABGPU_AI_TMPDIR/claude"',
                "&&",
                'ln -sf "$LABGPU_AI_TMPDIR/claude" "$LABGPU_AI_TMPDIR/claude-code"',
            ]
        )
    else:
        parts.extend(
            [
                'LABGPU_CLAUDE_SETTINGS="$LABGPU_AI_TMPDIR/claude-settings.json"',
                "&&",
                'export LABGPU_CLAUDE_SETTINGS',
                "&&",
                f"umask 077 && printf %s {shlex.quote(settings)} > \"$LABGPU_CLAUDE_SETTINGS\"",
                "&&",
                f"printf %s {shlex.quote(wrapper)} > \"$LABGPU_AI_TMPDIR/claude\"",
                "&&",
                'chmod 700 "$LABGPU_AI_TMPDIR/claude"',
                "&&",
                'ln -sf "$LABGPU_AI_TMPDIR/claude" "$LABGPU_AI_TMPDIR/claude-code"',
            ]
        )
    parts.extend([";", "fi"])
    return " ".join(parts)


def build_codex_app_setup(remote_env: dict[str, str]) -> str:
    remote_write = remote_env.get("LABGPU_AI_MODE") == "remote_write"
    auth = json.dumps(
        {
            "OPENAI_API_KEY": remote_env["OPENAI_API_KEY"],
            "auth_mode": "apikey",
        },
        separators=(",", ":"),
    )
    config = f'openai_base_url = "{remote_env["OPENAI_BASE_URL"]}"\n'
    wrapper = '#!/bin/sh\nexec "$LABGPU_REAL_CODEX" "$@"\n' if remote_write else '#!/bin/sh\nexport CODEX_HOME="$LABGPU_CODEX_HOME"\nexec "$LABGPU_REAL_CODEX" "$@"\n'
    parts = [
        'LABGPU_REAL_CODEX="${LABGPU_AI_CODEX_COMMAND:-}"',
        "&&",
        'if [ -z "$LABGPU_REAL_CODEX" ]; then LABGPU_REAL_CODEX="$(command -v codex || true)"; fi',
        ";",
        'case "$LABGPU_REAL_CODEX" in "~/"*) LABGPU_REAL_CODEX="${HOME}/${LABGPU_REAL_CODEX#~/}" ;; esac',
        ";",
        'export LABGPU_REAL_CODEX',
        ";",
        'if [ -n "$LABGPU_REAL_CODEX" ]; then',
    ]
    if remote_write:
        parts.extend(
            [
                f"printf %s {shlex.quote(wrapper)} > \"$LABGPU_AI_TMPDIR/codex\"",
                "&&",
                'chmod 700 "$LABGPU_AI_TMPDIR/codex"',
            ]
        )
    else:
        parts.extend(
            [
                'LABGPU_CODEX_HOME="$LABGPU_AI_TMPDIR/codex-home"',
                "&&",
                'mkdir -p "$LABGPU_CODEX_HOME"',
                "&&",
                'chmod 700 "$LABGPU_CODEX_HOME"',
                "&&",
                'export LABGPU_CODEX_HOME CODEX_HOME="$LABGPU_CODEX_HOME"',
                "&&",
                f"umask 077 && printf %s {shlex.quote(auth)} > \"$LABGPU_CODEX_HOME/auth.json\"",
                "&&",
                f"umask 077 && printf %s {shlex.quote(config)} > \"$LABGPU_CODEX_HOME/config.toml\"",
                "&&",
                f"printf %s {shlex.quote(wrapper)} > \"$LABGPU_AI_TMPDIR/codex\"",
                "&&",
                'chmod 700 "$LABGPU_AI_TMPDIR/codex"',
            ]
        )
    parts.extend([";", "fi"])
    return " ".join(parts)


def build_remote_config_override_setup(remote_env: dict[str, str]) -> str:
    app = remote_env.get("LABGPU_AI_APP")
    restore_script = build_remote_config_restore_script(app or "")
    parts = [
        'LABGPU_REMOTE_WRITE_BACKUP="$HOME/.labgpu/ai-config-backups/${LABGPU_AI_APP:-ai}-$(date +%Y%m%d-%H%M%S)-$$"',
        "&&",
        'mkdir -p "$LABGPU_REMOTE_WRITE_BACKUP"',
        "&&",
        'chmod 700 "$HOME/.labgpu" "$HOME/.labgpu/ai-config-backups" "$LABGPU_REMOTE_WRITE_BACKUP" 2>/dev/null || true',
        "&&",
        'export LABGPU_REMOTE_WRITE_BACKUP',
        "&&",
        f"umask 077 && printf %s {shlex.quote(restore_script)} > \"$LABGPU_REMOTE_WRITE_BACKUP/restore.sh\"",
        "&&",
        'chmod 700 "$LABGPU_REMOTE_WRITE_BACKUP/restore.sh"',
        "&&",
        build_remote_backup_function(),
        ";",
    ]
    if app == "claude":
        parts.append(build_claude_remote_config_override(remote_env))
    elif app == "codex":
        parts.append(build_codex_remote_config_override(remote_env))
    else:
        parts.append("false")
    parts.extend(
        [
            ";",
            'export LABGPU_REMOTE_WRITE_RESTORE="$LABGPU_REMOTE_WRITE_BACKUP/restore.sh"',
            ";",
            'printf "%s\\n" "LabGPU Remote Config Override: backed up previous config to $LABGPU_REMOTE_WRITE_BACKUP"',
            ";",
            'printf "%s\\n" "Restore with: sh $LABGPU_REMOTE_WRITE_RESTORE"',
        ]
    )
    return " ".join(parts)


def build_remote_backup_function() -> str:
    return (
        "labgpu_backup_config() { "
        'src="$1"; rel="$2"; dst="$LABGPU_REMOTE_WRITE_BACKUP/$rel"; '
        'mkdir -p "$(dirname "$dst")"; '
        'if [ -e "$src" ]; then cp -p "$src" "$dst"; else : > "$dst.missing"; fi; '
        "}"
    )


def build_claude_remote_config_override(remote_env: dict[str, str]) -> str:
    settings = json.dumps(
        {
            "env": {
                "ANTHROPIC_BASE_URL": remote_env["ANTHROPIC_BASE_URL"],
                "ANTHROPIC_API_KEY": remote_env["ANTHROPIC_API_KEY"],
            }
        },
        separators=(",", ":"),
    )
    return " ".join(
        [
            'mkdir -p "$HOME/.claude"',
            "&&",
            'chmod 700 "$HOME/.claude" 2>/dev/null || true',
            "&&",
            'labgpu_backup_config "$HOME/.claude/settings.json" ".claude/settings.json"',
            "&&",
            f"umask 077 && printf %s {shlex.quote(settings)} > \"$HOME/.claude/settings.json\"",
            "&&",
            'chmod 600 "$HOME/.claude/settings.json" 2>/dev/null || true',
        ]
    )


def build_codex_remote_config_override(remote_env: dict[str, str]) -> str:
    auth = json.dumps(
        {
            "OPENAI_API_KEY": remote_env["OPENAI_API_KEY"],
            "auth_mode": "apikey",
        },
        separators=(",", ":"),
    )
    config = f'openai_base_url = "{remote_env["OPENAI_BASE_URL"]}"\n'
    return " ".join(
        [
            'mkdir -p "$HOME/.codex"',
            "&&",
            'chmod 700 "$HOME/.codex" 2>/dev/null || true',
            "&&",
            'labgpu_backup_config "$HOME/.codex/auth.json" ".codex/auth.json"',
            "&&",
            'labgpu_backup_config "$HOME/.codex/config.toml" ".codex/config.toml"',
            "&&",
            f"umask 077 && printf %s {shlex.quote(auth)} > \"$HOME/.codex/auth.json\"",
            "&&",
            f"umask 077 && printf %s {shlex.quote(config)} > \"$HOME/.codex/config.toml\"",
            "&&",
            'chmod 600 "$HOME/.codex/auth.json" "$HOME/.codex/config.toml" 2>/dev/null || true',
        ]
    )


def build_remote_config_restore_script(app: str) -> str:
    restore_targets = {
        "claude": [(".claude/settings.json", "$HOME/.claude/settings.json")],
        "codex": [
            (".codex/auth.json", "$HOME/.codex/auth.json"),
            (".codex/config.toml", "$HOME/.codex/config.toml"),
        ],
    }.get(app, [])
    lines = [
        "#!/bin/sh",
        "set -eu",
        'backup_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"',
        "restore_one() {",
        '  rel="$1"',
        '  dst="$2"',
        '  src="$backup_dir/$rel"',
        '  mkdir -p "$(dirname "$dst")"',
        '  if [ -f "$src" ]; then',
        '    cp -p "$src" "$dst"',
        '    printf "%s\\n" "Restored $dst"',
        '  elif [ -f "$src.missing" ]; then',
        '    rm -f "$dst"',
        '    printf "%s\\n" "Removed LabGPU-created $dst"',
        "  else",
        '    printf "%s\\n" "No backup for $dst" >&2',
        "  fi",
        "}",
    ]
    for rel, dst in restore_targets:
        lines.append(f'restore_one {shlex.quote(rel)} "{dst}"')
    return "\n".join(lines) + "\n"


def build_aiswitch_helper() -> str:
    return """#!/bin/sh
set +x
cmd="${1:-status}"
base="${LABGPU_AI_BASE_URL:-}"
if [ -z "$base" ] && [ -n "${ANTHROPIC_BASE_URL:-}" ]; then base="$ANTHROPIC_BASE_URL"; fi
if [ -z "$base" ] && [ -n "${OPENAI_BASE_URL:-}" ]; then base="$OPENAI_BASE_URL"; fi
token="${LABGPU_AI_SESSION_TOKEN:-}"
if [ -z "$token" ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then token="$ANTHROPIC_API_KEY"; fi
if [ -z "$token" ] && [ -n "${OPENAI_API_KEY:-}" ]; then token="$OPENAI_API_KEY"; fi

app_wrapper() {
  case "${LABGPU_AI_APP:-}" in
    claude) command -v claude 2>/dev/null || command -v claude-code 2>/dev/null || printf missing ;;
    codex) command -v codex 2>/dev/null || printf missing ;;
    *) printf missing ;;
  esac
}

print_status() {
  printf '%s\\n' "LabGPU AI Session"
  printf 'Mode: %s\\n' "${LABGPU_AI_MODE:-unknown}"
  printf 'App: %s\\n' "${LABGPU_AI_APP:-unknown}"
  printf 'Provider: %s\\n' "${LABGPU_AI_PROVIDER:-unknown}"
  printf 'Base URL: %s\\n' "${base:-missing}"
  if [ -n "$token" ]; then
    printf '%s\\n' "Token: present (redacted)"
  else
    printf '%s\\n' "Token: missing"
  fi
  printf 'Working directory: %s\\n' "$(pwd 2>/dev/null || printf unknown)"
  printf 'App wrapper: %s\\n' "$(app_wrapper)"
}

case "$cmd" in
  status)
    print_status
    exit 0
    ;;
  doctor)
    print_status
    if [ -z "$base" ]; then
      printf '%s\\n' "Remote base URL: missing"
      exit 1
    fi
    if [ -z "$token" ]; then
      printf '%s\\n' "Session token: missing"
      exit 1
    fi
    if ! command -v curl >/dev/null 2>&1; then
      printf '%s\\n' "curl: missing"
      exit 1
    fi
    no_token_code="$(curl -sS -o /dev/null -w '%{http_code}' "$base/__labgpu/session" 2>/dev/null || true)"
    printf 'No-token gateway check: %s\\n' "${no_token_code:-failed}"
    tmp="${TMPDIR:-/tmp}/labgpu-aiswitch-session.$$"
    auth_code="$(curl -sS -o "$tmp" -w '%{http_code}' -H "x-api-key: $token" "$base/__labgpu/session" 2>/dev/null || true)"
    printf 'Authenticated session check: %s\\n' "${auth_code:-failed}"
    if [ "$auth_code" = "200" ]; then
      printf '%s\\n' "Gateway session: ok"
      cat "$tmp" 2>/dev/null || true
      printf '\\n'
    else
      printf '%s\\n' "Gateway session: failed"
    fi
    rm -f "$tmp"
    if [ "$no_token_code" = "401" ] && [ "$auth_code" = "200" ]; then
      exit 0
    fi
    exit 1
    ;;
  *)
    printf '%s\\n' "Usage: aiswitch [status|doctor]"
    exit 2
    ;;
esac
"""


def build_interactive_shell_exec(*, setup_ai_session: bool) -> str:
    if not setup_ai_session:
        return 'exec "${SHELL:-/bin/sh}" -i'
    return (
        'case "$(basename "${SHELL:-/bin/sh}")" in '
        'bash) exec "${SHELL:-/bin/bash}" --rcfile "$LABGPU_AI_TMPDIR/bashrc" -i ;; '
        'zsh) export ZDOTDIR="$LABGPU_AI_TMPDIR"; exec "${SHELL:-/bin/zsh}" -i ;; '
        '*) export PATH="$LABGPU_AI_TMPDIR:$PATH"; exec "${SHELL:-/bin/sh}" -i ;; '
        "esac"
    )


def validate_request(request: EnterServerAIRequest) -> None:
    if not is_safe_ssh_alias(request.server_alias):
        raise ValueError("Unsafe SSH alias.")
    if request.mode not in SUPPORTED_MODES:
        raise ValueError("Only Proxy Tunnel and Remote Config Override modes are available.")
    if request.ai_app not in SUPPORTED_AI_APPS:
        raise ValueError("Only Claude Code and Codex CLI AI sessions are available in this alpha.")
    if not request.provider_name.strip():
        raise ValueError(f"Current CC Switch provider is required for {ai_app_label(request.ai_app)}.")
    if request.ssh_target is not None and not request.ssh_target.strip():
        raise ValueError("SSH target is required.")
    if any(not isinstance(item, str) or not item for item in request.ssh_options):
        raise ValueError("SSH options must be non-empty argv strings.")
    validate_port(request.ccswitch_proxy_port, "CC Switch proxy port")
    validate_port(request.local_gateway_port, "Local gateway port")
    validate_port(request.remote_gateway_port, "Remote gateway port")
    validate_session_token(request.session_token)
    normalized_remote_cwd(request.remote_cwd)
    normalized_remote_path_prefixes(request.remote_path_prefixes)
    normalized_remote_command_path(request.claude_command)
    normalized_remote_command_path(request.codex_command)


def ai_app_label(app: str) -> str:
    return AI_APP_LABELS.get(str(app or "").strip().lower(), str(app or "AI app"))


def validate_port(value: int, label: str) -> None:
    if value < 1 or value > 65535:
        raise ValueError(f"{label} must be between 1 and 65535.")


def validate_session_token(value: str) -> None:
    if not SAFE_SESSION_TOKEN_RE.fullmatch(str(value or "")):
        raise ValueError("AI session token must be a LabGPU session token.")


def normalized_gpu_index(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "auto"}:
        return None
    if not SAFE_GPU_INDEX_RE.fullmatch(text):
        raise ValueError("GPU index must be none, auto, a number, or comma-separated numbers.")
    return text


def normalized_remote_cwd(value: str | None) -> str | None:
    raw = str(value or "")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ValueError("Remote working directory must be a single path.")
    text = raw.strip()
    if not text:
        return None
    if len(text) > 4096:
        raise ValueError("Remote working directory is too long.")
    if not (text.startswith("/") or text == "~" or text.startswith("~/")):
        raise ValueError("Remote working directory must be an absolute path or start with ~.")
    return text


def normalized_remote_path_prefixes(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    prefixes: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        path = normalized_remote_path(value, label="AI PATH entry")
        if path is None or path in seen:
            continue
        seen.add(path)
        prefixes.append(path)
    return tuple(prefixes)


def normalized_remote_command_path(value: str | None) -> str | None:
    return normalized_remote_path(value, label="Claude command path")


def normalized_remote_path(value: str | None, *, label: str) -> str | None:
    raw = str(value or "")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ValueError(f"{label} must be a single path.")
    text = raw.strip()
    if not text:
        return None
    if len(text) > 4096:
        raise ValueError(f"{label} is too long.")
    if text.startswith("$HOME/"):
        text = "~/" + text.removeprefix("$HOME/")
    if not (text.startswith("/") or text == "~" or text.startswith("~/")):
        raise ValueError(f"{label} must be an absolute path or start with ~.")
    return text


def build_path_export(remote_path_prefixes: tuple[str, ...] | list[str]) -> str:
    prefixes = normalized_remote_path_prefixes(list(remote_path_prefixes))
    if not prefixes:
        return ""
    entries = [shell_path_entry(path) for path in prefixes]
    return f"export PATH={':'.join(entries)}:$PATH"


def shell_path_entry(path: str) -> str:
    if path == "~":
        return "${HOME}"
    if path.startswith("~/"):
        return "${HOME}" + shlex.quote(path[1:])
    return shlex.quote(path)


def build_claude_command_probe(remote_path_prefixes: tuple[str, ...] | list[str] = DEFAULT_AI_PATH_PREFIXES, claude_command: str | None = None) -> str:
    parts: list[str] = []
    path_export = build_path_export(remote_path_prefixes)
    if path_export:
        parts.append(path_export)
    command_path = normalized_remote_command_path(claude_command)
    if command_path is not None:
        parts.append(f"if [ -x {shlex.quote(command_path)} ]; then printf '%s\\n' {shlex.quote(command_path)}; exit 0; fi")
    parts.extend(
        [
            "if command -v claude >/dev/null 2>&1; then command -v claude; exit 0; fi",
            "if command -v claude-code >/dev/null 2>&1; then command -v claude-code; exit 0; fi",
            "if command -v bash >/dev/null 2>&1; then bash -ic 'command -v claude || command -v claude-code' 2>/dev/null && exit 0; fi",
            "if [ -x \"$HOME/miniconda3/bin/claude\" ]; then printf '%s\\n' \"$HOME/miniconda3/bin/claude\"; exit 0; fi",
            "printf '%s\\n' 'claude not found in LabGPU launch PATH' >&2",
            "exit 127",
        ]
    )
    return "; ".join(parts)


def is_safe_ssh_alias(alias: str) -> bool:
    return bool(alias and not alias.startswith("-") and SAFE_SSH_ALIAS_RE.fullmatch(alias))
