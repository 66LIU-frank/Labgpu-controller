from __future__ import annotations

import json
import os
import socket
import sqlite3
import tomllib
from pathlib import Path
from typing import Any


CCSWITCH_DB = ".cc-switch/cc-switch.db"
CCSWITCH_SETTINGS = ".cc-switch/settings.json"
CCSWITCH_APP = "/Applications/CC Switch.app"
SUPPORTED_APPS = ("codex", "claude", "gemini", "openclaw")
SETTINGS_PROVIDER_KEYS = {
    "claude": "currentProviderClaude",
    "codex": "currentProviderCodex",
    "gemini": "currentProviderGemini",
    "openclaw": "currentProviderOpenclaw",
}
SWITCH_METHOD = "ccswitch_settings_and_db_state"
SWITCH_WARNING = (
    "LabGPU switches existing CC Switch providers by updating CC Switch current-provider state only. "
    "It does not read, store, or create provider API keys."
)


class CcSwitchError(RuntimeError):
    pass


def ccswitch_db_path(home: str | Path | None = None) -> Path:
    base = Path(home) if home is not None else Path(os.environ.get("HOME") or "~").expanduser()
    return base / CCSWITCH_DB


def ccswitch_settings_path(home: str | Path | None = None) -> Path:
    base = Path(home) if home is not None else Path(os.environ.get("HOME") or "~").expanduser()
    return base / CCSWITCH_SETTINGS


def read_ccswitch_summary(home: str | Path | None = None) -> dict[str, Any]:
    """Read non-secret CC Switch state for LabGPU jump hints."""
    db_path = ccswitch_db_path(home)
    summary: dict[str, Any] = {
        "available": False,
        "app_installed": Path(CCSWITCH_APP).exists(),
        "db_path": str(db_path),
        "providers": {},
        "proxy": {},
        "message": "CC Switch database was not found.",
    }
    if not db_path.exists():
        return summary
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
    except sqlite3.Error as exc:
        summary["message"] = f"Could not open CC Switch database: {exc}"
        return summary
    try:
        summary["providers"] = read_provider_state(conn, read_current_provider_settings(home))
        summary["proxy"] = read_proxy_state(conn)
        summary["available"] = True
        summary["message"] = "CC Switch detected. LabGPU reads provider names and proxy ports without reading secrets."
        return summary
    except sqlite3.Error as exc:
        summary["message"] = f"Could not read CC Switch database: {exc}"
        return summary
    finally:
        conn.close()


def read_provider_state(conn: sqlite3.Connection, settings_current: dict[str, str] | None = None) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    settings_current = settings_current or {}
    if not table_exists(conn, "providers"):
        return providers
    for app_type in SUPPORTED_APPS:
        rows = conn.execute(
            "SELECT id, name, is_current FROM providers WHERE app_type = ? ORDER BY is_current DESC, name COLLATE NOCASE",
            (app_type,),
        ).fetchall()
        if not rows:
            continue
        choices = [str(row[1] or "") for row in rows if row[1]]
        row_by_id = {str(row[0] or ""): row for row in rows if row[0]}
        db_current = next((str(row[1]) for row in rows if sqlite_truthy(row[2])), "")
        db_current_id = next((str(row[0]) for row in rows if sqlite_truthy(row[2])), "")
        settings_current_id = settings_current.get(app_type, "")
        effective_current_id = settings_current_id if settings_current_id in row_by_id else db_current_id
        effective_current_row = row_by_id.get(effective_current_id)
        current = str(effective_current_row[1] or "") if effective_current_row else db_current
        current_id = effective_current_id or db_current_id
        choice_items = [
            {"id": str(row[0] or ""), "name": str(row[1] or ""), "current": str(row[0] or "") == current_id}
            for row in rows
            if row[0] and row[1]
        ]
        providers[app_type] = {
            "current": current,
            "current_id": current_id,
            "current_source": "settings" if current_id and current_id == settings_current_id else "database",
            "db_current": db_current,
            "db_current_id": db_current_id,
            "choices": choices,
            "choices_detail": choice_items,
        }
    return providers


def switch_ccswitch_provider(app_type: str, provider_id: str, home: str | Path | None = None) -> dict[str, Any]:
    """Switch CC Switch's current provider for one app without reading secret settings."""
    app = str(app_type or "").strip().lower()
    selected_id = str(provider_id or "").strip()
    if app not in SUPPORTED_APPS:
        raise CcSwitchError("Unsupported CC Switch app type.")
    if not selected_id:
        raise CcSwitchError("Provider id is required.")
    db_path = ccswitch_db_path(home)
    if not db_path.exists():
        raise CcSwitchError("CC Switch database was not found.")
    try:
        conn = sqlite3.connect(db_path, timeout=1.0)
    except sqlite3.Error as exc:
        raise CcSwitchError(f"Could not open CC Switch database: {exc}") from exc
    try:
        if not table_exists(conn, "providers"):
            raise CcSwitchError("CC Switch providers table was not found.")
        row = conn.execute(
            "SELECT name, is_current FROM providers WHERE app_type = ? AND id = ?",
            (app, selected_id),
        ).fetchone()
        if not row:
            raise CcSwitchError("Selected provider was not found.")
        provider_name = str(row[0] or "")
        was_current = sqlite_truthy(row[1])
        with conn:
            conn.execute("UPDATE providers SET is_current = 0 WHERE app_type = ?", (app,))
            conn.execute("UPDATE providers SET is_current = 1 WHERE app_type = ? AND id = ?", (app, selected_id))
        try:
            write_current_provider_setting(app, selected_id, home)
        except OSError as exc:
            raise CcSwitchError(f"Could not update CC Switch settings: {exc}") from exc
        current_rows = conn.execute(
            "SELECT id FROM providers WHERE app_type = ? AND is_current = 1",
            (app,),
        ).fetchall()
        if [str(current[0]) for current in current_rows] != [selected_id]:
            raise CcSwitchError("CC Switch provider switch could not be verified.")
        return {
            "app": app,
            "provider_id": selected_id,
            "provider": provider_name,
            "changed": not was_current,
            "verified": True,
            "method": SWITCH_METHOD,
            "secret_access": False,
            "warning": SWITCH_WARNING,
            "message": (
                f"Switched {app} provider to {provider_name}."
                if not was_current
                else f"{provider_name} was already the current {app} provider."
            ),
        }
    except sqlite3.Error as exc:
        raise CcSwitchError(f"Could not update CC Switch provider: {exc}") from exc
    finally:
        conn.close()


def read_codex_provider_runtime(provider_id: str | None = None, home: str | Path | None = None) -> dict[str, Any]:
    """Read the selected Codex provider runtime config, including the local API key.

    This is intentionally separate from read_ccswitch_summary(), which remains
    non-secret. The returned API key is for local gateway forwarding only and
    must never be rendered in UI, logs, commands, or remote shell env.
    """
    return read_ai_provider_runtime("codex", provider_id, home)


def read_claude_provider_runtime(provider_id: str | None = None, home: str | Path | None = None) -> dict[str, Any]:
    """Read the selected Claude provider runtime config for local gateway forwarding."""
    return read_ai_provider_runtime("claude", provider_id, home)


def read_ai_provider_runtime(app_type: str, provider_id: str | None = None, home: str | Path | None = None) -> dict[str, Any]:
    app = str(app_type or "").strip().lower()
    if app not in {"claude", "codex"}:
        raise CcSwitchError("Only Claude and Codex provider runtime config is supported.")
    db_path = ccswitch_db_path(home)
    if not db_path.exists():
        raise CcSwitchError("CC Switch database was not found.")
    settings_current = read_current_provider_settings(home).get(app, "")
    selected_id = str(provider_id or settings_current or "").strip()
    try:
        conn = sqlite3.connect(db_path, timeout=1.0)
    except sqlite3.Error as exc:
        raise CcSwitchError(f"Could not open CC Switch database: {exc}") from exc
    try:
        if not table_exists(conn, "providers"):
            raise CcSwitchError("CC Switch providers table was not found.")
        if selected_id:
            row = conn.execute(
                "SELECT id, name, settings_config FROM providers WHERE app_type = ? AND id = ?",
                (app, selected_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, name, settings_config FROM providers WHERE app_type = ? AND is_current = 1",
                (app,),
            ).fetchone()
        if not row:
            raise CcSwitchError(f"Selected {app} provider was not found.")
        provider_id_value = str(row[0] or "")
        provider_name = str(row[1] or "")
        settings = parse_provider_settings_config(row[2])
        if app == "claude":
            runtime = claude_provider_runtime_from_settings(provider_id_value, provider_name, settings)
        else:
            runtime = codex_provider_runtime_from_settings(provider_id_value, provider_name, settings)
        runtime["app"] = app
        runtime["provider_id"] = provider_id_value
        runtime["provider"] = provider_name
        runtime["secret_access"] = True
        runtime["secret_scope"] = "local_gateway_only"
        return runtime
    except sqlite3.Error as exc:
        raise CcSwitchError(f"Could not read CC Switch {app} provider: {exc}") from exc
    finally:
        conn.close()


def claude_provider_runtime_from_settings(provider_id: str, provider_name: str, settings: dict[str, Any]) -> dict[str, Any]:
    env = normalize_provider_mapping(settings.get("env"))
    base_url = str(env.get("ANTHROPIC_BASE_URL") or env.get("base_url") or "").strip()
    api_key = str(env.get("ANTHROPIC_API_KEY") or "").strip()
    auth_token = str(env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    model = str(settings.get("model") or "").strip()
    if not base_url:
        raise CcSwitchError(f"Claude provider {provider_name or provider_id} does not have ANTHROPIC_BASE_URL in CC Switch.")
    if not api_key and not auth_token:
        raise CcSwitchError(f"Claude provider {provider_name or provider_id} does not have an API key/token in CC Switch.")
    headers = {"x-api-key": api_key} if api_key else {"Authorization": f"Bearer {auth_token}"}
    return {
        "base_url": base_url,
        "api_key": api_key or auth_token,
        "auth_header": "x-api-key" if api_key else "Authorization",
        "upstream_headers": headers,
        "model": model,
    }


def codex_provider_runtime_from_settings(provider_id: str, provider_name: str, settings: dict[str, Any]) -> dict[str, Any]:
    auth = normalize_provider_mapping(settings.get("auth"))
    config_text = str(settings.get("config") or "")
    config = parse_codex_config_toml(config_text)
    api_key = str(auth.get("OPENAI_API_KEY") or auth.get("api_key") or "").strip()
    base_url = codex_config_base_url(config)
    model = str(config.get("model") or "").strip()
    model_provider = str(config.get("model_provider") or "").strip()
    if not api_key:
        raise CcSwitchError(f"Codex provider {provider_name or provider_id} does not have an API key in CC Switch.")
    if not base_url:
        raise CcSwitchError(f"Codex provider {provider_name or provider_id} does not have base_url in CC Switch.")
    return {
        "base_url": base_url,
        "api_key": api_key,
        "upstream_headers": {"Authorization": f"Bearer {api_key}"},
        "model": model,
        "model_provider": model_provider,
    }


def parse_provider_settings_config(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def normalize_provider_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_codex_config_toml(value: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    try:
        data = tomllib.loads(value)
    except tomllib.TOMLDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def codex_config_base_url(config: dict[str, Any]) -> str:
    direct = str(config.get("base_url") or "").strip()
    if direct:
        return direct
    provider_name = str(config.get("model_provider") or "").strip()
    providers = config.get("model_providers")
    if isinstance(providers, dict) and provider_name:
        provider = providers.get(provider_name)
        if isinstance(provider, dict):
            return str(provider.get("base_url") or "").strip()
    return ""


def read_proxy_state(conn: sqlite3.Connection) -> dict[str, Any]:
    proxy: dict[str, Any] = {}
    if not table_exists(conn, "proxy_config"):
        return proxy
    rows = conn.execute(
        "SELECT app_type, listen_address, listen_port, proxy_enabled, enabled FROM proxy_config ORDER BY app_type",
    ).fetchall()
    for app_type, listen_address, listen_port, proxy_enabled, enabled in rows:
        port = int(listen_port or 0)
        proxy[str(app_type)] = {
            "listen_address": str(listen_address or "127.0.0.1"),
            "listen_port": port,
            "proxy_enabled": sqlite_truthy(proxy_enabled),
            "enabled": sqlite_truthy(enabled),
            "listening": is_local_proxy_listening(port) if port else False,
        }
    return proxy


def is_local_proxy_listening(port: int) -> bool | None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except PermissionError:
        return None
    except OSError:
        return False


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return bool(row)


def sqlite_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    return bool(value)


def read_current_provider_settings(home: str | Path | None = None) -> dict[str, str]:
    path = ccswitch_settings_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    current: dict[str, str] = {}
    for app, key in SETTINGS_PROVIDER_KEYS.items():
        value = str(data.get(key) or "").strip()
        if value:
            current[app] = value
    return current


def write_current_provider_setting(app_type: str, provider_id: str, home: str | Path | None = None) -> None:
    key = SETTINGS_PROVIDER_KEYS.get(app_type)
    if not key:
        return
    path = ccswitch_settings_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = provider_id
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".labgpu-tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
