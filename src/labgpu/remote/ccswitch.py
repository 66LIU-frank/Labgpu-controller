from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


CCSWITCH_DB = ".cc-switch/cc-switch.db"
CCSWITCH_APP = "/Applications/CC Switch.app"
SUPPORTED_APPS = ("codex", "claude", "gemini", "openclaw")


def ccswitch_db_path(home: str | Path | None = None) -> Path:
    base = Path(home) if home is not None else Path(os.environ.get("HOME") or "~").expanduser()
    return base / CCSWITCH_DB


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
        summary["providers"] = read_provider_state(conn)
        summary["proxy"] = read_proxy_state(conn)
        summary["available"] = True
        summary["message"] = "CC Switch detected. Provider switching is read-only in LabGPU for now."
        return summary
    except sqlite3.Error as exc:
        summary["message"] = f"Could not read CC Switch database: {exc}"
        return summary
    finally:
        conn.close()


def read_provider_state(conn: sqlite3.Connection) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    if not table_exists(conn, "providers"):
        return providers
    for app_type in SUPPORTED_APPS:
        rows = conn.execute(
            "SELECT name, is_current FROM providers WHERE app_type = ? ORDER BY is_current DESC, name COLLATE NOCASE",
            (app_type,),
        ).fetchall()
        if not rows:
            continue
        choices = [str(row[0] or "") for row in rows if row[0]]
        current = next((str(row[0]) for row in rows if row[1]), "")
        providers[app_type] = {
            "current": current,
            "choices": choices,
        }
    return providers


def read_proxy_state(conn: sqlite3.Connection) -> dict[str, Any]:
    proxy: dict[str, Any] = {}
    if not table_exists(conn, "proxy_config"):
        return proxy
    rows = conn.execute(
        "SELECT app_type, listen_address, listen_port, proxy_enabled, enabled FROM proxy_config ORDER BY app_type",
    ).fetchall()
    for app_type, listen_address, listen_port, proxy_enabled, enabled in rows:
        proxy[str(app_type)] = {
            "listen_address": str(listen_address or "127.0.0.1"),
            "listen_port": int(listen_port or 0),
            "proxy_enabled": bool(proxy_enabled),
            "enabled": bool(enabled),
        }
    return proxy


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return bool(row)
