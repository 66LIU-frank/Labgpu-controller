from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class VSCodeRemoteFolder:
    server_alias: str
    path: str
    label: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {
            "server_alias": self.server_alias,
            "path": self.path,
            "label": self.label,
            "source": self.source,
        }


def read_vscode_recent_remote_folders(*, home: Path | None = None, user_dirs: Iterable[Path] | None = None, limit: int = 80) -> list[dict[str, str]]:
    folders: list[VSCodeRemoteFolder] = []
    for user_dir in user_dirs or default_vscode_user_dirs(home=home):
        folders.extend(read_state_recent_folders(user_dir / "globalStorage" / "state.vscdb"))
        folders.extend(read_storage_recent_folders(user_dir / "storage.json"))
    deduped: list[VSCodeRemoteFolder] = []
    seen: set[tuple[str, str]] = set()
    for item in folders:
        key = (item.server_alias, item.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return [item.as_dict() for item in deduped]


def default_vscode_user_dirs(*, home: Path | None = None) -> list[Path]:
    root = Path(home or Path.home())
    return [
        root / "Library" / "Application Support" / "Code" / "User",
        root / "Library" / "Application Support" / "Code - Insiders" / "User",
        root / ".config" / "Code" / "User",
        root / "AppData" / "Roaming" / "Code" / "User",
    ]


def read_state_recent_folders(path: Path) -> list[VSCodeRemoteFolder]:
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute("SELECT value FROM ItemTable WHERE key = ?", ("history.recentlyOpenedPathsList",)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    folders: list[VSCodeRemoteFolder] = []
    for (raw_value,) in rows:
        folders.extend(extract_remote_folders(parse_json_value(raw_value), source="vscode-state"))
    return folders


def read_storage_recent_folders(path: Path) -> list[VSCodeRemoteFolder]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return extract_remote_folders(payload, source="vscode-storage")


def parse_json_value(value: object) -> object:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return {}
    try:
        return json.loads(value)
    except ValueError:
        return {}


def extract_remote_folders(payload: object, *, source: str) -> list[VSCodeRemoteFolder]:
    folders: list[VSCodeRemoteFolder] = []
    for item in iter_dicts(payload):
        folder_uri = item.get("folderUri") or item.get("uri")
        if not isinstance(folder_uri, str):
            continue
        parsed = parse_vscode_remote_folder_uri(
            folder_uri,
            label=str(item.get("label") or ""),
            remote_authority=str(item.get("remoteAuthority") or ""),
            source=source,
        )
        if parsed is not None:
            folders.append(parsed)
    return folders


def iter_dicts(value: object) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def parse_vscode_remote_folder_uri(uri: str, *, label: str = "", remote_authority: str = "", source: str = "vscode") -> VSCodeRemoteFolder | None:
    parsed = urlparse(uri)
    if parsed.scheme != "vscode-remote":
        return None
    authority = unquote(remote_authority or parsed.netloc)
    if not authority.startswith("ssh-remote+"):
        return None
    alias = authority.removeprefix("ssh-remote+").strip()
    path = unquote(parsed.path or "").strip()
    if not alias or not path.startswith("/"):
        return None
    return VSCodeRemoteFolder(
        server_alias=alias,
        path=path,
        label=label or f"{path} [SSH: {alias}]",
        source=source,
    )
