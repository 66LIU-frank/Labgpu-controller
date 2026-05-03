from __future__ import annotations

import json
import os
import socket
import sqlite3
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
