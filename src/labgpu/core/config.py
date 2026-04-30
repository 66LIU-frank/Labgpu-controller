from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from labgpu.core.paths import labgpu_home


@dataclass
class ServerEntry:
    name: str
    alias: str
    enabled: bool = True
    group: str = ""
    tags: list[str] = field(default_factory=list)
    disk_paths: list[str] = field(default_factory=list)
    shared_account: bool = False
    allow_stop_own_process: bool = True


@dataclass
class UIConfig:
    refresh_interval_seconds: int = 15
    safe_mode: bool = True


@dataclass
class LabGPUConfig:
    ui: UIConfig = field(default_factory=UIConfig)
    groups: list[str] = field(default_factory=list)
    servers: dict[str, ServerEntry] = field(default_factory=dict)


def config_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    return labgpu_home() / "config.toml"


def load_config(path: str | Path | None = None) -> LabGPUConfig:
    path = config_path(path)
    if not path.exists():
        return LabGPUConfig()
    return parse_config(path.read_text(encoding="utf-8", errors="replace"))


def parse_config(text: str) -> LabGPUConfig:
    config = LabGPUConfig()
    section: str | None = None
    server_name: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            server_name = None
            if section.startswith("servers."):
                server_name = section.removeprefix("servers.").strip('"')
                config.servers.setdefault(server_name, ServerEntry(name=server_name, alias=server_name))
            continue
        key, sep, raw_value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = parse_value(raw_value.strip())
        if section == "ui":
            if key == "refresh_interval_seconds" and isinstance(value, int):
                config.ui.refresh_interval_seconds = value
            elif key == "safe_mode" and isinstance(value, bool):
                config.ui.safe_mode = value
        elif section == "groups":
            if key == "names" and isinstance(value, list):
                config.groups = unique_strings(value)
        elif server_name:
            server = config.servers[server_name]
            if key == "alias" and isinstance(value, str):
                server.alias = value
            elif key == "enabled" and isinstance(value, bool):
                server.enabled = value
            elif key == "group" and isinstance(value, str):
                server.group = value
            elif key == "tags" and isinstance(value, list):
                server.tags = [str(item) for item in value]
            elif key == "disk_paths" and isinstance(value, list):
                server.disk_paths = [str(item) for item in value]
            elif key == "shared_account" and isinstance(value, bool):
                server.shared_account = value
            elif key == "allow_stop_own_process" and isinstance(value, bool):
                server.allow_stop_own_process = value
    return config


def write_config(config: LabGPUConfig, path: str | Path | None = None) -> Path:
    path = config_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(render_config(config), encoding="utf-8")
    tmp.replace(path)
    return path


def render_config(config: LabGPUConfig) -> str:
    lines = [
        "[ui]",
        f"refresh_interval_seconds = {config.ui.refresh_interval_seconds}",
        f"safe_mode = {render_bool(config.ui.safe_mode)}",
        "",
        "[groups]",
        f"names = {render_list(config_group_names(config))}",
        "",
    ]
    for name in sorted(config.servers):
        server = config.servers[name]
        lines.extend(
            [
                f"[servers.{quote_key(name)}]",
                f"enabled = {render_bool(server.enabled)}",
                f"alias = {quote_string(server.alias)}",
                f"group = {quote_string(server.group)}",
                f"tags = {render_list(server.tags)}",
                f"disk_paths = {render_list(server.disk_paths)}",
                f"shared_account = {render_bool(server.shared_account)}",
                f"allow_stop_own_process = {render_bool(server.allow_stop_own_process)}",
                "",
            ]
        )
    return "\n".join(lines)


def config_group_names(config: LabGPUConfig) -> list[str]:
    names = unique_strings(config.groups)
    for server in config.servers.values():
        group = server.group.strip()
        if group and group not in names:
            names.append(group)
    return sorted(names, key=str.lower)


def parse_value(raw: str) -> Any:
    value = raw.split("#", 1)[0].strip()
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"')
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        items: list[str] = []
        for item in inner.split(","):
            item = item.strip()
            if item.startswith('"') and item.endswith('"'):
                items.append(item[1:-1].replace('\\"', '"'))
            elif item:
                items.append(item)
        return items
    try:
        return int(value)
    except ValueError:
        return value


def quote_key(value: str) -> str:
    if all(ch.isalnum() or ch in "_-" for ch in value):
        return value
    return quote_string(value)


def quote_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_list(values: list[str]) -> str:
    return "[" + ", ".join(quote_string(value) for value in values) + "]"


def render_bool(value: bool) -> str:
    return "true" if value else "false"


def unique_strings(values: list[object]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        items.append(text)
        seen.add(text)
    return items
