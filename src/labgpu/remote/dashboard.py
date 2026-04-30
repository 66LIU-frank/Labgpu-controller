from __future__ import annotations

import html
import json
import secrets
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from labgpu.core.config import ServerEntry, config_group_names, load_config, write_config
from labgpu.remote.actions import open_ssh_terminal, stop_process
from labgpu.remote.alerts import all_alert_records, apply_alert_state, set_alert_status
from labgpu.remote.assistant import assistant_reply
from labgpu.remote.cache import read_server_cache, write_server_cache
from labgpu.remote.ccswitch import CcSwitchError, read_ccswitch_summary, switch_ccswitch_provider
from labgpu.remote.demo import fake_lab_data
from labgpu.remote.history import append_history, apply_history_evidence, read_history
from labgpu.remote.inventory import load_inventory
from labgpu.remote.probe import probe_host
from labgpu.remote import ranking
from labgpu.remote.ssh_config import SSHHost, append_ssh_host, default_ssh_config_path, parse_ssh_config, resolve_ssh_host
from labgpu.remote.state import alerts_for_server, annotate_server, build_overview, human_duration
from labgpu.remote.vscode_recent import read_vscode_recent_remote_folders
from labgpu.remote.workspace import failure_inbox_items, training_items

CACHE_TTL_SECONDS = 30
_REFRESH_LOCK = threading.Lock()
_REFRESHING_ALIASES: set[str] = set()


def serve(
    *,
    host: str,
    port: int,
    ssh_config: str | Path | None = None,
    names: list[str] | None = None,
    pattern: str | None = None,
    timeout: int = 8,
    open_browser: bool = False,
    allow_actions: bool = False,
    fake_lab: bool = False,
) -> None:
    ServerHandler.ssh_config = ssh_config
    ServerHandler.names = names
    ServerHandler.pattern = pattern
    ServerHandler.timeout = timeout
    ServerHandler.fake_lab = fake_lab
    ServerHandler.action_allowed = is_loopback(host) or allow_actions
    ServerHandler.action_token = secrets.token_urlsafe(24)
    if host == "0.0.0.0":
        print("Warning: LabGPU Home has no authentication in this version.")
    if not ServerHandler.action_allowed:
        print("LabGPU Home actions are disabled for this bind address.")
    server = ThreadingHTTPServer((host, port), ServerHandler)
    url = f"http://{host}:{port}"
    print(f"LabGPU Home: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def collect_servers(
    *,
    ssh_config: str | Path | None = None,
    names: list[str] | None = None,
    pattern: str | None = None,
    group: str | None = None,
    timeout: int = 8,
    fake_lab: bool = False,
    use_cache: bool = False,
    background_refresh: bool = False,
) -> dict[str, object]:
    if fake_lab:
        data = fake_lab_data()
        hosts = data.get("hosts") if isinstance(data.get("hosts"), list) else []
        if names:
            selected = set(names)
            hosts = [host for host in hosts if isinstance(host, dict) and host.get("alias") in selected]
        if pattern:
            needle = pattern.lower()
            hosts = [host for host in hosts if isinstance(host, dict) and needle in str(host.get("alias") or "").lower()]
        if group:
            hosts = [host for host in hosts if isinstance(host, dict) and group_matches(host.get("group"), group)]
        data["hosts"] = hosts
        data["count"] = len(hosts)
        data["overview"] = build_overview(hosts)
        data["overview"]["all_alert_items"] = data["overview"].get("alert_items", [])
        data["inventory_mode"] = "demo"
        return data
    saved_config = load_config()
    using_saved_inventory = not names and not pattern and any(entry.enabled for entry in saved_config.servers.values())
    hosts = load_inventory(ssh_config=ssh_config, names=names, pattern=pattern)
    hosts = filter_inventory_group(hosts, group)
    if not hosts:
        return {"hosts": [], "count": 0, "error": "no SSH hosts selected"}
    hosts = [resolve_ssh_host(host) for host in hosts]
    host_order = {host.alias: index for index, host in enumerate(hosts)}
    if use_cache:
        results, refresh_hosts = collect_cached_results(hosts)
        if background_refresh and refresh_hosts:
            schedule_background_refresh(refresh_hosts, timeout=timeout)
    else:
        results = collect_live_results(hosts, timeout=timeout)
        refresh_hosts = []
    results.sort(key=lambda item: host_order.get(str(item.get("alias") or ""), len(host_order)))
    overview = build_overview(results)
    scoped_servers = {str(item.get("alias") or "") for item in results if isinstance(item, dict) and item.get("alias")}
    enriched_alerts = apply_alert_state(list(overview.get("alert_items") or []), scoped_servers=scoped_servers)
    active_alerts = [alert for alert in enriched_alerts if alert.get("status") == "active"]
    overview["alert_items"] = active_alerts
    overview["all_alert_items"] = all_alert_records()
    overview["alerts"] = len(active_alerts)
    overview["critical_alerts"] = sum(1 for item in active_alerts if item.get("severity") == "error")
    overview["warning_alerts"] = sum(1 for item in active_alerts if item.get("severity") == "warning")
    payload: dict[str, object] = {
        "hosts": results,
        "count": len(results),
        "overview": overview,
        "error": None,
        "inventory_mode": "saved" if using_saved_inventory else "ssh_config",
    }
    if use_cache:
        payload["cache_mode"] = "snapshot"
        payload["cache_ttl_seconds"] = CACHE_TTL_SECONDS
        payload["refreshing_hosts"] = [host.alias for host in refresh_hosts]
        payload["oldest_cache_age_seconds"] = max((int(item.get("cache_age_seconds") or 0) for item in results), default=0)
    return payload


def collect_live_results(hosts: list[SSHHost], *, timeout: int) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=min(16, len(hosts))) as executor:
        futures = {executor.submit(probe_host, host, timeout=timeout): host for host in hosts}
        for future in as_completed(futures):
            host = futures[future]
            result = prepare_probe_result(future.result(), alias=host.alias)
            if result.get("online") and not result.get("probe_incomplete"):
                write_server_cache(result)
                append_history(result)
            else:
                cached = read_server_cache(host.alias)
                if cached:
                    result["cached"] = cached
                    result["last_seen"] = cached.get("probed_at")
            results.append(result)
    return results


def collect_cached_results(hosts: list[SSHHost]) -> tuple[list[dict[str, object]], list[SSHHost]]:
    results: list[dict[str, object]] = []
    refresh_hosts: list[SSHHost] = []
    for host in hosts:
        cached = read_server_cache(host.alias)
        if cached:
            result = dict(cached)
            result.update(host_identity(host))
            result["from_cache"] = True
            result["cache_snapshot_at"] = cached.get("probed_at")
            age = cache_age_seconds(cached.get("probed_at"))
            if age is not None:
                result["cache_age_seconds"] = age
            result = prepare_probe_result(result, alias=host.alias)
            if age is None or age > CACHE_TTL_SECONDS:
                refresh_hosts.append(host)
            results.append(result)
            continue
        result = {
            **host_identity(host),
            "online": False,
            "mode": "cache_miss",
            "from_cache": True,
            "cache_miss": True,
            "error": "No cached probe yet. Refreshing in background.",
            "gpus": [],
            "processes": [],
            "disks": [],
        }
        result = prepare_probe_result(result, alias=host.alias)
        refresh_hosts.append(host)
        results.append(result)
    return results, refresh_hosts


def prepare_probe_result(result: dict[str, object], *, alias: str) -> dict[str, object]:
    result = annotate_server(result)
    result = apply_history_evidence(result, read_history(alias))
    result["alerts"] = alerts_for_server(result)
    return result


def host_identity(host: SSHHost) -> dict[str, object]:
    return {
        "alias": host.alias,
        "hostname": host.hostname,
        "user": host.user,
        "port": host.port,
        "proxyjump": host.proxyjump,
        "group": host.group,
        "tags": host.tags,
        "disk_paths": host.disk_paths,
        "shared_account": host.shared_account,
        "allow_stop_own_process": host.allow_stop_own_process,
    }


def schedule_background_refresh(hosts: list[SSHHost], *, timeout: int) -> None:
    with _REFRESH_LOCK:
        pending = [host for host in hosts if host.alias not in _REFRESHING_ALIASES]
        for host in pending:
            _REFRESHING_ALIASES.add(host.alias)
    if not pending:
        return
    thread = threading.Thread(target=background_refresh_worker, args=(pending, timeout), daemon=True)
    thread.start()


def background_refresh_worker(hosts: list[SSHHost], timeout: int) -> None:
    try:
        collect_live_results(hosts, timeout=timeout)
    finally:
        with _REFRESH_LOCK:
            for host in hosts:
                _REFRESHING_ALIASES.discard(host.alias)


def cache_age_seconds(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()))


def filter_inventory_group(hosts: list[SSHHost], group: str | None) -> list[SSHHost]:
    group = str(group or "").strip()
    if not group or group == "all":
        return hosts
    return [host for host in hosts if group_matches(host.group, group)]


def group_matches(value: object, selected: str | None) -> bool:
    selected = str(selected or "").strip()
    if not selected or selected == "all":
        return True
    group = str(value or "").strip()
    if selected == "__ungrouped__":
        return not group
    return group == selected


def configured_groups() -> list[dict[str, str]]:
    config = load_config()
    groups = config_group_names(config)
    if not groups:
        return []
    has_ungrouped = any(not entry.group.strip() for entry in config.servers.values())
    items = [{"value": group, "label": group} for group in groups]
    if has_ungrouped:
        items.append({"value": "__ungrouped__", "label": "Ungrouped"})
    return items


def add_config_group(config: object, group: str) -> None:
    groups = getattr(config, "groups", None)
    if not isinstance(groups, list):
        return
    if group and group not in groups:
        groups.append(group)


def current_ccswitch_provider(summary: dict[str, object], app: str) -> str:
    providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
    provider = providers.get(app) if isinstance(providers.get(app), dict) else {}
    return str(provider.get("current") or "").strip()


class ServerHandler(BaseHTTPRequestHandler):
    ssh_config: str | Path | None = None
    names: list[str] | None = None
    pattern: str | None = None
    timeout: int = 8
    action_allowed: bool = False
    action_token: str = ""
    fake_lab: bool = False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._html(render_index(self._data(parsed.query)))
        elif parsed.path == "/gpus":
            self._html(render_gpus_page(self._data(parsed.query)))
        elif parsed.path == "/me":
            self._html(render_me_page(self._data(parsed.query)))
        elif parsed.path == "/servers":
            self._html(render_servers_page(self._data(parsed.query)))
        elif parsed.path == "/alerts":
            self._html(render_alerts_page(self._data(parsed.query)))
        elif parsed.path == "/groups":
            self._html(render_groups_page())
        elif parsed.path == "/settings":
            self._html(render_settings_page(ssh_config=self.ssh_config))
        elif parsed.path == "/providers":
            self._html(render_providers_page())
        elif parsed.path == "/assistant":
            self._html(render_assistant_page(self._data(parsed.query)))
        elif parsed.path == "/api/overview":
            self._json(self._data(parsed.query))
        elif parsed.path == "/api/servers":
            self._json(self._data(parsed.query))
        elif parsed.path == "/api/integrations/ccswitch":
            self._json(read_ccswitch_summary())
        elif parsed.path == "/api/integrations/vscode/recent-folders":
            self._json({"folders": read_vscode_recent_remote_folders()})
        elif parsed.path.startswith("/servers/"):
            alias = unquote(parsed.path.removeprefix("/servers/")).strip("/")
            self._html(render_detail(self._data_for_alias(alias, parsed.query)))
        elif parsed.path.startswith("/api/servers/"):
            alias = unquote(parsed.path.removeprefix("/api/servers/")).strip("/")
            self._json(self._data_for_alias(alias, parsed.query))
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if (
            len(parts) == 6
            and parts[:2] == ["api", "servers"]
            and parts[3] == "processes"
            and parts[5] in {"stop", "force-stop"}
        ):
            try:
                pid = int(parts[4])
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST, "invalid pid")
                return
            self._stop_process(unquote(parts[2]), pid, force=parts[5] == "force-stop")
            return
        if len(parts) == 4 and parts[:2] == ["api", "servers"] and parts[3] == "open-ssh":
            self._open_ssh_terminal(unquote(parts[2]))
            return
        if len(parts) == 4 and parts[:2] == ["api", "alerts"] and parts[3] in {"dismiss", "snooze", "activate"}:
            self._alert_action(unquote(parts[2]), parts[3])
            return
        if parts == ["api", "settings", "import-ssh"]:
            self._settings_import()
            return
        if parts == ["api", "settings", "add-server"]:
            self._settings_add_server()
            return
        if parts == ["api", "settings", "groups"]:
            self._settings_groups()
            return
        if parts == ["api", "settings", "groups", "delete"]:
            self._settings_delete_groups()
            return
        if parts == ["api", "integrations", "ccswitch", "switch"]:
            self._ccswitch_switch()
            return
        if parts == ["api", "assistant", "chat"]:
            self._assistant_chat()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _data(self, query: str) -> dict[str, object]:
        params = parse_qs(query)
        names = self.names
        scope_mode = "command" if self.names else ""
        if params.get("hosts"):
            names = split_hosts(params["hosts"][0])
            scope_mode = "url"
        pattern = params.get("pattern", [self.pattern])[0]
        if pattern and not scope_mode:
            scope_mode = "pattern"
        group = params.get("group", [""])[0].strip()
        refresh = truthy(params.get("refresh", ["0"])[0])
        data = collect_servers(
            ssh_config=self.ssh_config,
            names=names,
            pattern=pattern,
            group=group,
            timeout=self.timeout,
            fake_lab=self.fake_lab,
            use_cache=not refresh,
            background_refresh=not refresh,
        )
        data["scope_mode"] = scope_mode
        data["scope_hosts"] = names or []
        data["scope_pattern"] = pattern or ""
        data["scope_group"] = group
        data["server_groups"] = configured_groups()
        data["ui"] = {
            "q": params.get("q", [""])[0].strip(),
            "group": group,
            "min_mem_gb": params.get("min_mem_gb", [""])[0].strip(),
            "model": params.get("model", [""])[0].strip(),
            "tag": params.get("tag", [""])[0].strip(),
            "server": params.get("server", [""])[0].strip(),
            "health": params.get("health", [""])[0].strip(),
            "severity": params.get("severity", [""])[0].strip(),
            "online": params.get("online", [""])[0].strip(),
            "free": params.get("free", [""])[0].strip(),
            "alerts": params.get("alerts", [""])[0].strip(),
            "mine": params.get("mine", [""])[0].strip(),
            "sort": params.get("sort", [""])[0].strip(),
            "availability": params.get("availability", [""])[0].strip(),
            "alert_status": params.get("alert_status", ["active"])[0].strip() or "active",
        }
        return data

    def _data_for_alias(self, alias: str, query: str = "") -> dict[str, object]:
        params = parse_qs(query)
        refresh = truthy(params.get("refresh", ["0"])[0])
        return collect_servers(
            ssh_config=self.ssh_config,
            names=[alias],
            pattern=None,
            timeout=self.timeout,
            fake_lab=self.fake_lab,
            use_cache=not refresh,
            background_refresh=not refresh,
        )

    def _stop_process(self, alias: str, pid: int, *, force: bool) -> None:
        if not self.action_allowed:
            self.send_error(HTTPStatus.FORBIDDEN, "actions disabled")
            return
        if self.headers.get("X-LabGPU-Action-Token") != self.action_token:
            self.send_error(HTTPStatus.FORBIDDEN, "invalid action token")
            return
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, OSError):
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return
        host = resolve_ssh_host(load_inventory(ssh_config=self.ssh_config, names=[alias])[0])
        result = stop_process(
            host,
            pid=pid,
            expected_user=str(payload.get("expected_user") or ""),
            expected_start_time=payload.get("expected_start_time"),
            expected_command_hash=payload.get("expected_command_hash"),
            force=force,
            timeout=self.timeout,
        )
        self._json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT)

    def _open_ssh_terminal(self, alias: str) -> None:
        if not self.action_allowed:
            self.send_error(HTTPStatus.FORBIDDEN, "actions disabled")
            return
        if not self._valid_action_token():
            self.send_error(HTTPStatus.FORBIDDEN, "invalid action token")
            return
        if alias not in known_ssh_aliases(self.ssh_config):
            self.send_error(HTTPStatus.NOT_FOUND, "unknown SSH alias")
            return
        payload = self._read_body_payload()
        proxy_port = first_value(payload.get("proxy_port"))
        local_proxy_port = first_value(payload.get("local_proxy_port"))
        remote_proxy_port = first_value(payload.get("remote_proxy_port"))
        agent = str(first_value(payload.get("agent")) or "none")
        ai_mode = str(first_value(payload.get("ai_mode")) or "").strip()
        gpu_index = first_value(payload.get("gpu_index"))
        remote_cwd = str(first_value(payload.get("remote_cwd")) or "").strip()
        provider_name = str(first_value(payload.get("provider_name")) or "").strip()
        ccswitch_provider_id = str(first_value(payload.get("ccswitch_provider_id")) or "").strip()
        ccswitch_switch = None
        if agent == "claude" and ai_mode == "proxy_tunnel":
            summary = read_ccswitch_summary()
            provider_name = current_ccswitch_provider(summary, "claude")
            if not provider_name:
                self._json(
                    {"ok": False, "result": "missing_provider", "message": "Current CC Switch Claude provider was not found. Switch Claude provider in CC Switch first."},
                    status=HTTPStatus.CONFLICT,
                )
                return
        if ccswitch_provider_id:
            try:
                ccswitch_switch = switch_ccswitch_provider(agent, ccswitch_provider_id)
            except CcSwitchError as exc:
                self._json({"ok": False, "result": "ccswitch_switch_failed", "message": str(exc)}, status=HTTPStatus.CONFLICT)
                return
        host = resolve_ssh_host(load_inventory(ssh_config=self.ssh_config, names=[alias])[0])
        result = open_ssh_terminal(
            host,
            proxy_port=proxy_port,
            local_proxy_port=local_proxy_port,
            remote_proxy_port=remote_proxy_port,
            agent=agent,
            ai_mode=ai_mode or None,
            provider_name=provider_name or None,
            gpu_index=gpu_index,
            remote_cwd=remote_cwd or None,
        )
        if ccswitch_switch:
            result["ccswitch"] = ccswitch_switch
        self._json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT)

    def _ccswitch_switch(self) -> None:
        if not self.action_allowed:
            self.send_error(HTTPStatus.FORBIDDEN, "actions disabled")
            return
        if not self._valid_action_token():
            self.send_error(HTTPStatus.FORBIDDEN, "invalid action token")
            return
        payload = self._read_body_payload()
        app = str(first_value(payload.get("app")) or "").strip()
        provider_id = str(first_value(payload.get("provider_id")) or "").strip()
        try:
            switched = switch_ccswitch_provider(app, provider_id)
        except CcSwitchError as exc:
            self._json({"ok": False, "message": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        self._json({"ok": True, "switched": switched, "summary": read_ccswitch_summary()})

    def _alert_action(self, key: str, action: str) -> None:
        if not self._valid_action_token():
            self.send_error(HTTPStatus.FORBIDDEN, "invalid action token")
            return
        status = {"dismiss": "dismissed", "snooze": "snoozed", "activate": "active"}[action]
        try:
            record = set_alert_status(key, status)
        except KeyError:
            self.send_error(HTTPStatus.NOT_FOUND, "alert not found")
            return
        except ValueError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._json({"ok": True, "alert": record})

    def _settings_import(self) -> None:
        payload = self._read_body_payload()
        if not self._valid_action_token(payload):
            self.send_error(HTTPStatus.FORBIDDEN, "invalid action token")
            return
        aliases = payload.get("aliases") or payload.get("alias") or []
        if isinstance(aliases, str):
            aliases = split_hosts(aliases) or []
        aliases = [str(alias).strip() for alias in aliases if str(alias).strip()]
        if not aliases:
            self.send_error(HTTPStatus.BAD_REQUEST, "no aliases selected")
            return
        tags = split_csv(str(first_value(payload.get("tags")) or ""))
        group_value = first_value(payload.get("group"))
        group = str(group_value or "").strip()
        disk_paths = split_csv(str(first_value(payload.get("disk_paths")) or "")) or ["/", "/home", "/data", "/scratch", "/mnt", "/nvme"]
        shared_account = truthy(first_value(payload.get("shared_account")))
        allow_stop = truthy(first_value(payload.get("allow_stop_own_process")), default=True)

        config = load_config()
        if group:
            add_config_group(config, group)
        for entry in config.servers.values():
            entry.enabled = False
        for alias in aliases:
            entry = next((item for item in config.servers.values() if item.alias == alias), None) or ServerEntry(name=alias, alias=alias)
            entry.enabled = True
            if group_value is not None:
                entry.group = group
            entry.tags = tags
            entry.disk_paths = disk_paths
            entry.shared_account = shared_account
            entry.allow_stop_own_process = allow_stop
            config.servers[entry.name] = entry
        write_config(config)
        self._json({"ok": True, "imported": aliases})

    def _settings_add_server(self) -> None:
        if not self.action_allowed:
            self.send_error(HTTPStatus.FORBIDDEN, "actions disabled")
            return
        payload = self._read_body_payload()
        if not self._valid_action_token(payload):
            self.send_error(HTTPStatus.FORBIDDEN, "invalid action token")
            return
        alias = str(first_value(payload.get("alias")) or "").strip()
        hostname = str(first_value(payload.get("hostname")) or "").strip()
        user = str(first_value(payload.get("user")) or "").strip()
        port = str(first_value(payload.get("port")) or "").strip()
        proxyjump = str(first_value(payload.get("proxyjump")) or "").strip()
        identity_file = str(first_value(payload.get("identity_file")) or "").strip()
        write_ssh = truthy(first_value(payload.get("write_ssh_config")), default=True)
        tags = split_csv(str(first_value(payload.get("tags")) or ""))
        group_value = first_value(payload.get("group"))
        group = str(group_value or "").strip()
        disk_paths = split_csv(str(first_value(payload.get("disk_paths")) or "")) or ["/", "/home", "/data", "/scratch", "/mnt", "/nvme"]
        shared_account = truthy(first_value(payload.get("shared_account")))
        allow_stop = truthy(first_value(payload.get("allow_stop_own_process")), default=True)
        if not alias:
            self.send_error(HTTPStatus.BAD_REQUEST, "alias is required")
            return
        if write_ssh and not hostname:
            self.send_error(HTTPStatus.BAD_REQUEST, "hostname is required when writing SSH config")
            return
        ssh_config_path = Path(self.ssh_config).expanduser() if self.ssh_config else default_ssh_config_path()
        written_path: Path | None = None
        backup_path: Path | None = None
        if write_ssh:
            try:
                written_path, backup_path = append_ssh_host(
                    alias=alias,
                    hostname=hostname,
                    user=user or None,
                    port=port or None,
                    proxyjump=proxyjump or None,
                    identity_file=identity_file or None,
                    path=ssh_config_path,
                )
            except ValueError as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except OSError as exc:
                self.send_error(HTTPStatus.CONFLICT, f"failed to write SSH config: {exc}")
                return
        config = load_config()
        if group:
            add_config_group(config, group)
        entry = config.servers.get(alias) or ServerEntry(name=alias, alias=alias)
        entry.enabled = True
        if group_value is not None:
            entry.group = group
        entry.tags = tags
        entry.disk_paths = disk_paths
        entry.shared_account = shared_account
        entry.allow_stop_own_process = allow_stop
        config.servers[entry.name] = entry
        write_config(config)
        self._json(
            {
                "ok": True,
                "alias": alias,
                "ssh_config": str(written_path or ssh_config_path),
                "ssh_config_written": write_ssh,
                "backup": str(backup_path) if backup_path else "",
            }
        )

    def _settings_groups(self) -> None:
        payload = self._read_body_payload()
        if not self._valid_action_token(payload):
            self.send_error(HTTPStatus.FORBIDDEN, "invalid action token")
            return
        aliases = payload.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        aliases = [str(alias).strip() for alias in aliases if str(alias).strip()]
        group_name = str(first_value(payload.get("group_name")) or "").strip()
        if not group_name and not aliases:
            self.send_error(HTTPStatus.BAD_REQUEST, "group name is required")
            return
        config = load_config()
        if group_name:
            add_config_group(config, group_name)
        updated: list[str] = []
        for alias in aliases:
            entry = next((item for item in config.servers.values() if item.alias == alias), None)
            if not entry:
                continue
            entry.group = group_name
            updated.append(alias)
        write_config(config)
        self._json({"ok": True, "updated": updated, "group": group_name})

    def _settings_delete_groups(self) -> None:
        payload = self._read_body_payload()
        if not self._valid_action_token(payload):
            self.send_error(HTTPStatus.FORBIDDEN, "invalid action token")
            return
        names = payload.get("groups") or []
        if isinstance(names, str):
            names = [names]
        groups = {str(name).strip() for name in names if str(name).strip()}
        if not groups:
            self.send_error(HTTPStatus.BAD_REQUEST, "no groups selected")
            return
        config = load_config()
        config.groups = [name for name in config.groups if name not in groups]
        unassigned: list[str] = []
        for entry in config.servers.values():
            if entry.group in groups:
                entry.group = ""
                unassigned.append(entry.alias)
        write_config(config)
        self._json({"ok": True, "deleted": sorted(groups), "unassigned": unassigned})

    def _assistant_chat(self) -> None:
        payload = self._read_body_payload()
        message = str(payload.get("message") or "")
        options = payload.get("assistant") if isinstance(payload.get("assistant"), dict) else {}
        data = self._data("")
        self._json(assistant_reply(data, message, options=options))

    def _read_body_payload(self) -> dict[str, object]:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}
        parsed = parse_qs(body.decode("utf-8", errors="replace"))
        return {key: values if len(values) > 1 else values[0] for key, values in parsed.items()}

    def _valid_action_token(self, payload: dict[str, object] | None = None) -> bool:
        token = self.headers.get("X-LabGPU-Action-Token")
        if not token and payload:
            token = str(payload.get("action_token") or "")
        return bool(token and token == self.action_token)

    def _html(self, body: str) -> None:
        self._send("text/html; charset=utf-8", body.encode("utf-8"))

    def _json(self, value: object, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send("application/json; charset=utf-8", json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8"), status=status)

    def _send(self, content_type: str, body: bytes, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def render_index(data: dict[str, object]) -> str:
    hosts = data.get("hosts") or []
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    cards = "".join(render_host_card(host, compact=True) for host in preview_list(hosts, len(hosts) if isinstance(hosts, list) else 0))
    if not cards:
        cards = f"<p class='muted'>{esc(data.get('error') or 'No hosts found.')}</p>"
    mode = str(data.get("inventory_mode") or "ssh_config")
    server_note = scope_note(data) or {
        "saved": "Showing your saved enabled servers. Change this list in Settings.",
        "demo": "Showing built-in demo servers.",
    }.get(mode, "Showing SSH hosts from your config. Save selected hosts in Settings to make the home page faster.")
    return page(
        "LabGPU Home",
        f"""
        <section class="toolbar">
          <div>
            <h1>LabGPU Home</h1>
            <p>Personal GPU workspace for students using shared SSH servers.</p>
          </div>
        </section>
        {render_group_bar(data, path='/')}
        {render_overview(overview)}
        {render_train_now(overview, ui=ui, limit=4)}
        {render_my_training(training_items(hosts, overview), limit=8, view_all=page_url('/me', ui))}
        {render_failure_inbox(failure_inbox_items(hosts, overview), ui=ui, limit=8)}
        {render_alerts(overview.get('alert_items') if isinstance(overview, dict) else [], limit=8, view_all=page_url('/alerts', ui), title='Problems')}
        <section class="panel"><div class="section-head"><h2>Servers</h2><a href="/settings">Choose home servers</a></div><p class="muted">{esc(server_note)}</p><div class="grid compact">{cards}</div></section>
        """,
        status=render_data_status(data),
    )


def render_gpus_page(data: dict[str, object]) -> str:
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    return page(
        "Train Now",
        f"""
        <section class="toolbar">
          <div>
            <h1>Train Now</h1>
            <p>Rank GPUs across SSH hosts by GPU availability, free VRAM, model, load, and tags.</p>
          </div>
        </section>
        {render_group_bar(data, path='/gpus')}
        {render_filters(ui, kind='gpus')}
        {render_gpu_watch_panel(ui)}
        {render_gpu_finder(overview, ui)}
        """,
        status=render_data_status(data),
    )


def render_me_page(data: dict[str, object]) -> str:
    hosts = data.get("hosts") or []
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    return page(
        "My Training",
        f"""
        <section class="toolbar">
          <div>
            <h1>My Training</h1>
            <p>Your LabGPU runs, adopted runs, and agentless GPU processes across SSH servers.</p>
          </div>
        </section>
        {render_group_bar(data, path='/me')}
        {render_process_filters(ui)}
        {render_my_training(training_items(hosts, overview), ui=ui)}
        {render_my_processes(overview.get('my_process_items') if isinstance(overview, dict) else [], ui=ui, title='Agentless Own GPU Processes')}
        """,
        status=render_data_status(data),
    )


def render_servers_page(data: dict[str, object]) -> str:
    hosts = data.get("hosts") or []
    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    filtered = filter_hosts(hosts, ui)
    cards = "".join(render_host_card(host) for host in filtered) or "<p class='muted'>No server matched the current filters.</p>"
    return page(
        "Servers",
        f"""
        <section class="toolbar">
          <div>
            <h1>Servers</h1>
            <p>Configured SSH GPU servers, health, disks, and free/busy GPUs.</p>
          </div>
        </section>
        {render_group_bar(data, path='/servers')}
        {render_server_filters(ui)}
        <section class="grid">{cards}</section>
        """,
        status=render_data_status(data),
    )


def render_alerts_page(data: dict[str, object]) -> str:
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    alerts = scoped_alert_items(data, overview.get("all_alert_items") if isinstance(overview, dict) else [])
    return page(
        "Alerts",
        f"""
        <section class="toolbar">
          <div>
            <h1>Alerts</h1>
            <p>Disk, SSH, GPU, and process conditions that need attention.</p>
          </div>
        </section>
        {render_group_bar(data, path='/alerts')}
        {render_alert_filters(ui)}
        {render_alerts(alerts, ui=ui, title='All Alerts')}
        """,
        status=render_data_status(data),
    )


def render_settings_page(*, ssh_config: str | Path | None = None) -> str:
    config = load_config()
    ssh_hosts = parse_ssh_config(ssh_config)
    ssh_config_path = Path(ssh_config).expanduser() if ssh_config else default_ssh_config_path()
    saved_enabled = {entry.alias for entry in config.servers.values() if entry.enabled}
    host_rows = "".join(
        f"<tr><td><label><input type='checkbox' name='aliases' value='{esc(host.alias)}' {'checked' if host.alias in saved_enabled else ''}> <code>{esc(host.alias)}</code></label></td><td>{esc(host.hostname or '-')}</td><td>{esc(host.user or '-')}</td><td>{esc(host.port or 22)}</td><td><a href='/servers/{esc(host.alias)}'>Test connection</a></td></tr>"
        for host in ssh_hosts
    ) or "<tr><td colspan='5' class='muted'>No SSH hosts found.</td></tr>"
    saved_entries = list(config.servers.values())
    server_rows = "".join(
        f"<tr><td><code>{esc(entry.alias)}</code></td><td>{esc(entry.enabled)}</td><td>{esc(entry.group or '-')}</td><td>{esc(join_values(entry.tags))}</td><td>{esc(join_values(entry.disk_paths))}</td><td>{esc(entry.shared_account)}</td><td>{esc(entry.allow_stop_own_process)}</td></tr>"
        for entry in saved_entries
    ) or "<tr><td colspan='7' class='muted'>No saved LabGPU server inventory yet.</td></tr>"
    return page(
        "Settings",
        f"""
        <section class="toolbar">
          <div>
            <h1>Settings</h1>
            <p>Choose which SSH GPU servers appear in LabGPU Home.</p>
          </div>
        </section>
        <section class="panel">
          <h2>Saved Servers</h2>
          <p class="muted">These enabled servers are shown on LabGPU Home by default.</p>
          <table><tr><th>Alias</th><th>Enabled</th><th>Group</th><th>Tags</th><th>Disk paths</th><th>Shared account</th><th>Stop own process</th></tr>{server_rows}</table>
        </section>
        <section class="panel">
          <h2>Add Server</h2>
          <p class="muted">For a new student setup, fill the basics below. LabGPU can add the SSH alias to <code>{esc(ssh_config_path)}</code> and save it to <code>~/.labgpu/config.toml</code>.</p>
          <form id="settings-add-server">
            <input type="hidden" name="action_token" value="{esc(ServerHandler.action_token)}">
            <div class="filters">
              <label>Alias <input name="alias" required placeholder="alpha_liu"></label>
              <label>HostName or IP <input name="hostname" required placeholder="gpu.example.edu"></label>
              <label>SSH user <input name="user" placeholder="student"></label>
              <label>Tags <input name="tags" placeholder="A100,training"></label>
              <label><input type="checkbox" name="write_ssh_config" value="1" checked> Write to SSH config</label>
              <button class="button" type="submit">Add server</button>
            </div>
            <details>
              <summary>Advanced SSH options</summary>
              <div class="filters">
                <label>Port <input name="port" placeholder="22"></label>
                <label>ProxyJump <input name="proxyjump" placeholder="bastion"></label>
                <label>IdentityFile <input name="identity_file" placeholder="~/.ssh/id_ed25519"></label>
                <label>Disk paths <input name="disk_paths" value="/,/home,/data,/scratch,/mnt,/nvme"></label>
                <label><input type="checkbox" name="shared_account" value="1"> Shared Linux account</label>
                <label><input type="checkbox" name="allow_stop_own_process" value="1" checked> Allow stop own process</label>
              </div>
            </details>
          </form>
          <p class="muted">SSH auth is still handled by your normal SSH setup. For automatic probing, <code>ssh ALIAS</code> should work without an interactive password prompt, usually through an SSH key or ssh-agent. IdentityFile is optional and only needed if you use a specific private key. Existing SSH aliases are not overwritten, and LabGPU writes a backup before appending to an existing SSH config.</p>
        </section>
        <section class="panel">
          <h2>Import From SSH Config</h2>
          <p class="muted">Select the SSH aliases you want on LabGPU Home. Saved enabled hosts are the default probe set, so choosing fewer servers makes the home page faster. Test connection opens a probe page that auto-detects GPU model, disks, load, and LabGPU Enhanced Mode when available.</p>
          <form id="settings-import">
            <input type="hidden" name="action_token" value="{esc(ServerHandler.action_token)}">
            <div class="filters">
              <label>Tags <input name="tags" placeholder="A100,training"></label>
              <label>Disk paths <input name="disk_paths" value="/,/home,/data,/scratch,/mnt,/nvme"></label>
              <label><input type="checkbox" name="shared_account" value="1"> Shared Linux account</label>
              <label><input type="checkbox" name="allow_stop_own_process" value="1" checked> Allow stop own process</label>
              <button class="button" type="submit">Save selected hosts</button>
            </div>
            <table><tr><th>Alias</th><th>HostName</th><th>User</th><th>Port</th><th>Probe</th></tr>{host_rows}</table>
          </form>
        </section>
        <section class="panel">
          <h2>Interface</h2>
          <label class="inline-setting"><input type="checkbox" id="show-json-toggle"> Show JSON/API links</label>
          <p class="muted">Show raw JSON/API links in the top-right controls. Most users can leave this off.</p>
        </section>
        """,
    )


def render_groups_page() -> str:
    config = load_config()
    saved_entries = sorted(config.servers.values(), key=lambda entry: entry.alias.lower())
    groups = config_group_names(config)
    group_counts = {group: 0 for group in groups}
    for entry in saved_entries:
        if entry.group in group_counts:
            group_counts[entry.group] += 1
    group_links = "".join(f"<a class='group-chip' href='/?group={quote(group)}'>{esc(group)} · {esc(group_counts.get(group, 0))}</a>" for group in groups)
    group_summary = group_links or "<span class='muted'>No groups yet.</span>"
    delete_rows = "".join(
        f"<tr><td><label><input type='checkbox' name='groups' value='{esc(group)}'> <code>{esc(group)}</code></label></td><td>{esc(group_counts.get(group, 0))} server(s)</td><td><a href='/?group={quote(group)}'>View group</a></td></tr>"
        for group in groups
    ) or "<tr><td colspan='3' class='muted'>No groups to delete.</td></tr>"
    group_rows = "".join(
        f"<tr><td><label><input type='checkbox' name='aliases' value='{esc(entry.alias)}'> <code>{esc(entry.alias)}</code></label></td><td>{esc('enabled' if entry.enabled else 'disabled')}</td><td>{esc(entry.group or '-')}</td><td>{esc(join_values(entry.tags))}</td></tr>"
        for entry in saved_entries
    ) or "<tr><td colspan='4' class='muted'>Save servers in Settings first, then create groups here.</td></tr>"
    group_options = "<option value=''>Ungrouped</option>" + "".join(f"<option value='{esc(group)}'>{esc(group)}</option>" for group in groups)
    return page(
        "Server Groups",
        f"""
        <section class="toolbar">
          <div>
            <h1>Server Groups</h1>
            <p>Create a group name, select existing saved servers, and switch views by group when you train.</p>
          </div>
        </section>
        <section class="panel">
          <div class="section-head"><h2>Existing Groups</h2><a href="/settings">Add servers</a></div>
          <div class="groupbar">{group_summary}</div>
        </section>
        <div class="group-actions">
          <div class="group-side">
            <section class="panel group-create">
              <h2>Create Group</h2>
              <p class="muted">Create a group name first, even if you do not want to add servers yet.</p>
              <form class="settings-groups-form">
                <input type="hidden" name="action_token" value="{esc(ServerHandler.action_token)}">
                <div class="filters">
                  <label>Group name <input name="group_name" required placeholder="liusuu / AlphaLab / off-campus"></label>
                  <button class="button" type="submit">Create group</button>
                </div>
              </form>
            </section>
            <section class="panel group-delete">
              <h2>Delete Group Names</h2>
              <p class="muted">Deleting a group name does not delete servers. Servers in that group are moved back to ungrouped.</p>
              <form id="settings-delete-groups">
                <input type="hidden" name="action_token" value="{esc(ServerHandler.action_token)}">
                <table><tr><th>Group</th><th>Members</th><th>View</th></tr>{delete_rows}</table>
                <div class="actions" style="margin-top:10px"><button class="button danger" type="submit">Delete selected group names</button></div>
              </form>
            </section>
          </div>
          <section class="panel group-update">
            <h2>Update Group Members</h2>
            <p class="muted">Choose an existing group or Ungrouped, then select saved servers to move there.</p>
            <form class="settings-groups-form">
              <input type="hidden" name="action_token" value="{esc(ServerHandler.action_token)}">
              <div class="filters">
                <label>Target group <select name="group_name">{group_options}</select></label>
                <button class="button" type="submit">Move selected servers to this group</button>
              </div>
              <table><tr><th>Select</th><th>Status</th><th>Current group</th><th>Tags</th></tr>{group_rows}</table>
            </form>
          </section>
        </div>
        """,
    )


def render_providers_page() -> str:
    summary = read_ccswitch_summary()
    apps = [
        ("claude", "Claude Code"),
        ("codex", "Codex CLI"),
        ("gemini", "Gemini CLI"),
        ("openclaw", "OpenClaw Agent"),
    ]
    cards = "".join(render_provider_card(summary, app, label) for app, label in apps)
    detected = bool(summary.get("available"))
    detected_class = "ok" if detected else "warning"
    detected_label = "detected" if detected else "not detected"
    return page(
        "AI Providers",
        f"""
        <section class="toolbar">
          <div>
            <h1>AI Providers</h1>
            <p>Provider state for remote AI CLI sessions. LabGPU reads names, current selections, and proxy ports only.</p>
          </div>
          <span class="badge {detected_class}">CC Switch {detected_label}</span>
        </section>
        <section class="panel">
          <div class="section-head"><h2>Remote Session Modes</h2><a href="/gpus">Open from Train Now</a></div>
          <div class="grid compact">
            <div class="card">
              <div class="card-head"><h3>Proxy Tunnel</h3><span class="badge ok">recommended</span></div>
              <p class="muted">Open SSH with a reverse tunnel to a local proxy. API keys stay on this laptop or in the local provider tool.</p>
            </div>
            <div class="card">
              <div class="card-head"><h3>Remote Write</h3><span class="badge warning">advanced</span></div>
              <p class="muted">Writing provider keys to remote <code>~/.claude</code>, <code>~/.codex</code>, or <code>~/.gemini</code> is intentionally not a default Alpha workflow.</p>
            </div>
          </div>
        </section>
        <section class="panel">
          <div class="section-head"><h2>CC Switch Providers</h2><a href="/api/integrations/ccswitch">JSON</a></div>
          <p class="muted">{esc(summary.get("message") or "")}</p>
          <div class="grid compact">{cards}</div>
        </section>
        <section class="panel">
          <h2>Open A Remote AI Session</h2>
          <p class="muted">Use any server or GPU card's Enter Server action, choose Claude Code and Proxy Tunnel, then open the terminal. LabGPU uses the current CC Switch Claude provider and opens an SSH tunnel; it does not copy API keys to the server.</p>
        </section>
        <section class="panel">
          <h2>Last Launched AI Sessions</h2>
          <p class="muted">Browser-local launch history only. It does not prove the terminal or tunnel is still alive.</p>
          <table><tr><th>Server</th><th>Folder</th><th>App / Provider</th><th>Proxy Tunnel</th><th>GPU</th><th>Started</th></tr><tbody id="ai-session-rows"><tr><td colspan="6" class="muted">No AI sessions launched from this browser yet.</td></tr></tbody></table>
        </section>
        """,
        json_href="/api/integrations/ccswitch",
    )


def render_provider_card(summary: dict[str, object], app: str, label: str) -> str:
    providers = summary.get("providers") if isinstance(summary.get("providers"), dict) else {}
    proxy = summary.get("proxy") if isinstance(summary.get("proxy"), dict) else {}
    provider = providers.get(app) if isinstance(providers.get(app), dict) else {}
    proxy_config = proxy.get(app) if isinstance(proxy.get(app), dict) else {}
    current = str(provider.get("current") or "-")
    choices = provider.get("choices_detail") if isinstance(provider.get("choices_detail"), list) else []
    choice_rows = "".join(
        f"<tr><td>{esc(item.get('name') or '-')}</td><td>{esc('current' if item.get('current') else '')}</td></tr>"
        for item in choices
        if isinstance(item, dict)
    ) or "<tr><td colspan='2' class='muted'>No providers found.</td></tr>"
    port = proxy_config.get("listen_port") if isinstance(proxy_config, dict) else None
    enabled = bool(proxy_config.get("enabled") or proxy_config.get("proxy_enabled")) if isinstance(proxy_config, dict) else False
    listening = proxy_config.get("listening") if isinstance(proxy_config, dict) else None
    proxy_label = f"{proxy_config.get('listen_address') or '127.0.0.1'}:{port}" if port else "-"
    proxy_class = "error" if enabled and listening is False else ("ok" if enabled else ("warning" if port else ""))
    proxy_state = "not listening" if enabled and listening is False else ("enabled" if enabled else ("configured" if port else "not configured"))
    tcp_state = "listening" if listening is True else ("not listening" if listening is False and port else ("unknown" if enabled and port else "-"))
    return f"""
    <article class="card">
      <div class="card-head">
        <div>
          <h3>{esc(label)}</h3>
          <p>Current provider: <strong>{esc(current)}</strong></p>
        </div>
        <span class="badge {esc(proxy_class)}">{esc(proxy_state)}</span>
      </div>
      <div class="meta">
        <span><span>Local proxy</span> <code>{esc(proxy_label)}</code></span>
        <span><span>TCP check</span> <code>{esc(tcp_state)}</code></span>
        <span><span>App id</span> <code>{esc(app)}</code></span>
      </div>
      <table><tr><th>Provider</th><th>Status</th></tr>{choice_rows}</table>
    </article>
    """


def render_assistant_page(data: dict[str, object]) -> str:
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    return page(
        "LabGPU Assistant",
        f"""
        <section class="toolbar">
          <div>
            <h1>LabGPU Assistant</h1>
            <p>Chat with your GPU workspace. Read-only and copy-only in this alpha.</p>
          </div>
        </section>
        {render_group_bar(data, path='/assistant')}
        <section class="panel assistant-config">
          <div class="section-head"><h2>Assistant API</h2><span class="muted">Optional BYO OpenAI-compatible API</span></div>
          <label class="inline-setting"><input type="checkbox" id="assistant-use-api"> Use my API for answers</label>
          <div class="filters">
            <label>API URL <input id="assistant-api-url" placeholder="https://api.openai.com/v1/chat/completions"></label>
            <label>Model <input id="assistant-model" placeholder="gpt-4o-mini or provider model"></label>
            <label>API key <input id="assistant-api-key" type="password" placeholder="sk-..."></label>
            <label><input type="checkbox" id="assistant-remember-key"> Remember key in this browser</label>
          </div>
          <p class="muted">Default mode uses local LabGPU rules only. API mode sends redacted workspace context to your configured endpoint and stays read-only/copy-only. LabGPU does not save this key to <code>~/.labgpu/config.toml</code>.</p>
        </section>
        <section class="panel assistant-panel">
          <div class="assistant-examples">
            <button class="small" type="button" data-assistant-example="Find me a 24G A100 for python train.py --config configs/sft.yaml">Find a GPU</button>
            <button class="small" type="button" data-assistant-example="Where are my training jobs?">Where are my jobs?</button>
            <button class="small" type="button" data-assistant-example="What failed or looks suspicious?">Explain failures</button>
            <button class="small" type="button" data-assistant-example="Copy debug context for my run">Debug context</button>
          </div>
          <div id="assistant-chat" class="assistant-chat">
            <div class="assistant-message assistant-message-system">
              Ask for a GPU recommendation, your current runs, suspicious failures, or a debug context command.
              I will not execute SSH commands; I only explain and generate copyable plans.
            </div>
          </div>
          <form id="assistant-form" class="assistant-form">
            <textarea id="assistant-input" name="message" rows="3" placeholder="Example: Find me a 40G A100 for python train.py --config configs/sft.yaml"></textarea>
            <button class="button" type="submit">Ask LabGPU</button>
          </form>
        </section>
        {render_train_now(overview, ui=data.get("ui") if isinstance(data.get("ui"), dict) else {}, limit=3)}
        """,
        status=render_data_status(data),
    )


def render_overview(overview: dict[str, object]) -> str:
    online_class = "ok" if overview.get("online_servers") else "error"
    available = int(overview.get("available_gpus") or 0)
    total_gpus = int(overview.get("total_gpus") or 0)
    available_class = "ok" if available else ("warning" if total_gpus else "unknown")
    critical = int(overview.get("critical_alerts") or 0)
    warnings = int(overview.get("warning_alerts") or 0)
    alert_class = "error" if critical else ("warning" if warnings else "ok")
    return f"""
    <section class="health">
      <div class="summary-card {online_class}"><strong>{esc(overview.get('online_servers', 0))}/{esc(overview.get('total_servers', 0))}</strong><span>online servers</span></div>
      <div class="summary-card {available_class}"><strong>{esc(available)}/{esc(total_gpus)}</strong><span>available GPUs</span></div>
      <div class="summary-card ok"><strong>{esc(overview.get('my_processes', 0))}</strong><span>my training processes</span></div>
      <div class="summary-card {alert_class}"><strong>{esc(overview.get('alerts', 0))}</strong><span>alerts · {esc(critical)} critical / {esc(warnings)} warning</span></div>
    </section>
    """


def render_data_status(data: dict[str, object]) -> str:
    if data.get("cache_mode") != "snapshot":
        return ""
    hosts = data.get("hosts") if isinstance(data.get("hosts"), list) else []
    missing = sum(1 for host in hosts if isinstance(host, dict) and host.get("cache_miss"))
    refreshing = data.get("refreshing_hosts") if isinstance(data.get("refreshing_hosts"), list) else []
    oldest = int(data.get("oldest_cache_age_seconds") or 0)
    age = human_duration(oldest) if oldest else "just now"
    parts: list[str] = []
    if missing:
        parts.append(f"<span><span>Servers missing cache</span>: {esc(missing)}</span>")
    parts.append(f"<span><span>Cached data age</span>: {esc(age)}</span>")
    message = " ".join(parts)
    scope = scope_note(data)
    scope_html = f"<span class='badge warning' title='{esc(scope)}'>Scoped</span>" if scope else ""
    return (
        "<div class='cache-status'>"
        "<span class='badge'>Cached page</span>"
        f"<span class='cache-message'>{message}</span>"
        f"{scope_html}"
        "</div>"
    )


def render_group_bar(data: dict[str, object], *, path: str) -> str:
    groups = data.get("server_groups") if isinstance(data.get("server_groups"), list) else []
    if not groups:
        return ""
    current = str(data.get("scope_group") or "").strip()
    chips = [group_chip("All", path, "", active=not current or current == "all")]
    for group in groups:
        if not isinstance(group, dict):
            continue
        value = str(group.get("value") or "").strip()
        label = str(group.get("label") or value).strip()
        if not value:
            continue
        chips.append(group_chip(label, path, value, active=current == value))
    return (
        "<section class='groupbar'>"
        "<span class='muted'>Server group</span>"
        f"{''.join(chips)}"
        "</section>"
    )


def group_chip(label: str, path: str, value: str, *, active: bool) -> str:
    href = path if not value else f"{path}?group={quote(value)}"
    active_class = " active" if active else ""
    return f"<a class='group-chip{active_class}' href='{href}'>{esc(label)}</a>"


def scope_note(data: dict[str, object]) -> str:
    mode = str(data.get("scope_mode") or "")
    hosts = data.get("scope_hosts") if isinstance(data.get("scope_hosts"), list) else []
    host_text = ", ".join(str(host) for host in hosts if str(host).strip())
    if mode == "command":
        return "Showing hosts fixed by this UI launch. Settings are saved, but this view will stay scoped until you restart without --hosts."
    if mode == "url":
        suffix = f": {host_text}" if host_text else ""
        return f"Showing hosts from the URL filter{suffix}."
    if mode == "pattern":
        pattern = str(data.get("scope_pattern") or "")
        return f"Showing hosts matching pattern: {pattern}."
    return ""


def render_filters(ui: dict[str, object], *, kind: str = "all") -> str:
    q = str(ui.get("q") or "")
    model = str(ui.get("model") or "")
    min_mem = str(ui.get("min_mem_gb") or "")
    tag = str(ui.get("tag") or "")
    sort = str(ui.get("sort") or "")
    return f"""
    <form class="filters" method="get">
      {group_hidden(ui)}
      <label>Search <input name="q" value="{esc(q)}" placeholder="alpha, A100, lsg"></label>
      <label>Model <input name="model" value="{esc(model)}" placeholder="A100 / 4090 / H800"></label>
      <label>Min free GB <input name="min_mem_gb" value="{esc(min_mem)}" placeholder="24"></label>
      <label>Tag <input name="tag" value="{esc(tag)}" placeholder="training"></label>
      <label>Sort
        <select name="sort">
          {option('', 'Recommended', sort)}
          {option('memory', 'Free memory', sort)}
          {option('load', 'Server load', sort)}
          {option('model', 'Model', sort)}
        </select>
      </label>
      <button class="button" type="submit">Filter</button>
      <a class="button" href="{esc(page_url('/' + kind if kind != 'all' else '/', ui))}">Clear</a>
    </form>
    """


def render_gpu_watch_panel(ui: dict[str, object]) -> str:
    model = str(ui.get("model") or "")
    min_mem = str(ui.get("min_mem_gb") or "")
    tag = str(ui.get("tag") or "")
    return f"""
    <section class="panel">
      <div class="section-head"><h2>Notify me when GPU is free</h2><span class="muted">Browser notification only</span></div>
      <div class="filters">
        <label>Model <input id="watch-model" value="{esc(model)}" placeholder="A100 / 4090"></label>
        <label>Min free GB <input id="watch-min-mem" value="{esc(min_mem)}" placeholder="24"></label>
        <label>Server tag <input id="watch-tag" value="{esc(tag)}" placeholder="training"></label>
        <button class="button" type="button" id="watch-enable">Notify me</button>
        <button class="button" type="button" id="watch-clear">Clear watch</button>
      </div>
      <p class="muted">When a matching GPU appears after refresh, the browser notification says: LabGPU - alpha_shi GPU 0 is available.</p>
      <p class="muted" id="watch-status">No browser watch configured.</p>
    </section>
    """


def render_process_filters(ui: dict[str, object]) -> str:
    q = str(ui.get("q") or "")
    server = str(ui.get("server") or "")
    health = str(ui.get("health") or "")
    return f"""
    <form class="filters" method="get">
      {group_hidden(ui)}
      <label>Search <input name="q" value="{esc(q)}" placeholder="command, PID, user"></label>
      <label>Server <input name="server" value="{esc(server)}" placeholder="alpha_liu"></label>
      <label>Health
        <select name="health">
          {option('', 'Any', health)}
          {option('healthy', 'Healthy', health)}
          {option('suspected_idle', 'Suspected idle', health)}
          {option('io_wait', 'IO wait', health)}
          {option('zombie', 'Zombie', health)}
        </select>
      </label>
      <button class="button" type="submit">Filter</button>
      <a class="button" href="{esc(page_url('/me', ui))}">Clear</a>
    </form>
    """


def render_server_filters(ui: dict[str, object]) -> str:
    q = str(ui.get("q") or "")
    tag = str(ui.get("tag") or "")
    online = str(ui.get("online") or "")
    free = str(ui.get("free") or "")
    alerts = str(ui.get("alerts") or "")
    mine = str(ui.get("mine") or "")
    return f"""
    <form class="filters" method="get">
      {group_hidden(ui)}
      <label>Search <input name="q" value="{esc(q)}" placeholder="alias, model, user"></label>
      <label>Tag <input name="tag" value="{esc(tag)}" placeholder="A100"></label>
      <label><input type="checkbox" name="online" value="1" {checked(online)}> Online only</label>
      <label><input type="checkbox" name="free" value="1" {checked(free)}> Has free GPU</label>
      <label><input type="checkbox" name="alerts" value="1" {checked(alerts)}> Has alerts</label>
      <label><input type="checkbox" name="mine" value="1" {checked(mine)}> Has my processes</label>
      <button class="button" type="submit">Filter</button>
      <a class="button" href="{esc(page_url('/servers', ui))}">Clear</a>
    </form>
    """


def render_alert_filters(ui: dict[str, object]) -> str:
    severity = str(ui.get("severity") or "")
    q = str(ui.get("q") or "")
    status = str(ui.get("alert_status") or "active")
    return f"""
    <form class="filters" method="get">
      {group_hidden(ui)}
      <label>Search <input name="q" value="{esc(q)}" placeholder="server, message"></label>
      <label>Severity
        <select name="severity">
          {option('', 'Any', severity)}
          {option('error', 'Critical', severity)}
          {option('warning', 'Warning', severity)}
          {option('info', 'Info', severity)}
        </select>
      </label>
      <label>Status
        <select name="alert_status">
          {option('active', 'Active', status)}
          {option('dismissed', 'Dismissed', status)}
          {option('resolved', 'Resolved', status)}
          {option('all', 'All', status)}
        </select>
      </label>
      <button class="button" type="submit">Filter</button>
      <a class="button" href="{esc(page_url('/alerts', ui))}">Clear</a>
    </form>
    """


def group_hidden(ui: dict[str, object]) -> str:
    group = str(ui.get("group") or "").strip()
    if not group or group == "all":
        return ""
    return f"<input type='hidden' name='group' value='{esc(group)}'>"


def render_train_now(overview: dict[str, object], *, ui: dict[str, object] | None = None, limit: int | None = None) -> str:
    filter_ui = dict(ui or {})
    filter_ui["availability"] = "available"
    items = filter_gpu_items(overview.get("gpu_items") or [], filter_ui)
    if limit is not None:
        items = items[:limit]
    if not items:
        return render_available_gpus([], filter_ui, title="Train Now / Recommended GPUs", counts=overview)
    cards = "".join(render_gpu_recommendation_card(item) for item in items)
    return (
        "<section class='panel'>"
        f"<div class='section-head'><h2>Train Now / Recommended GPUs</h2><a href='{esc(page_url('/gpus', filter_ui))}'>View all</a></div>"
        "<p class='muted'>Copy an SSH command, CUDA_VISIBLE_DEVICES value, or LabGPU launch snippet.</p>"
        f"<div class='gpu-list'>{cards}</div></section>"
    )


def render_my_training(
    items: object,
    *,
    ui: dict[str, object] | None = None,
    title: str = "My Runs",
    limit: int | None = None,
    view_all: str | None = None,
) -> str:
    rows = filter_training_items(items, ui or {})
    if limit is not None:
        rows = rows[:limit]
    head = section_head(title, view_all)
    if not rows:
        return f"<section class='panel'>{head}<p class='muted'>No LabGPU run or own GPU process found yet.</p></section>"
    body = "".join(
        f"<tr><td>{esc(item.get('name') or '-')}</td><td><a href='/servers/{esc(item.get('host'))}'>{esc(item.get('host') or '-')}</a></td><td>{esc(item.get('gpu') or '-')}</td><td>{esc(item.get('pid') or '-')}</td><td>{esc(item.get('runtime') or '-')}</td><td>{esc(item.get('last_log_time') or '-')}</td><td>{esc(item.get('status') or '-')}</td><td><span class='badge {esc(health_badge(item.get('health')))}'>{esc(item.get('health') or '-')}</span></td><td>{esc(short(item.get('diagnosis') or '-', 80))}</td><td>{render_training_actions(item)}</td></tr>"
        for item in rows
        if isinstance(item, dict)
    )
    return f"<section class='panel'>{head}<table><tr><th>Name</th><th>Host</th><th>GPU</th><th>PID</th><th>Runtime</th><th>Last log</th><th>Status</th><th>Health</th><th>Diagnosis</th><th>Action</th></tr>{body}</table></section>"


def render_failure_inbox(items: object, *, ui: dict[str, object] | None = None, limit: int | None = None) -> str:
    rows = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return "<section class='panel'><div class='section-head'><h2>Failed or Suspicious Runs</h2></div><p class='muted'>No failed run, suspected idle process, or failure signal found.</p></section>"
    body = "".join(
        f"<tr><td>{esc(item.get('source') or item.get('kind') or '-')}</td><td>{esc(item.get('name') or '-')}</td><td>{esc(item.get('host') or '-')}</td><td>{esc(item.get('gpu') or '-')}</td><td>{esc(item.get('pid') or '-')}</td><td><span class='badge {esc(health_badge(item.get('status')))}'>{esc(item.get('status') or '-')}</span></td><td>{esc(short(item.get('diagnosis') or '-', 120))}</td><td>{render_training_actions(item)}</td></tr>"
        for item in rows
    )
    return f"<section class='panel'><div class='section-head'><h2>Failed or Suspicious Runs</h2><a href='{esc(page_url('/me', ui or {}))}'>My training</a></div><table><tr><th>Source</th><th>Name</th><th>Host</th><th>GPU</th><th>PID</th><th>Status</th><th>Signal</th><th>Action</th></tr>{body}</table></section>"


def filter_training_items(items: object, ui: dict[str, object]) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    q = str(ui.get("q") or "").lower()
    server = str(ui.get("server") or "").lower()
    health = str(ui.get("health") or "").lower()
    out: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        haystack = " ".join(
            [
                str(item.get("name") or ""),
                str(item.get("host") or ""),
                str(item.get("pid") or ""),
                str(item.get("command") or ""),
                str(item.get("health") or ""),
                str(item.get("diagnosis") or ""),
            ]
        ).lower()
        if q and q not in haystack:
            continue
        if server and server not in str(item.get("host") or "").lower():
            continue
        if health and health not in str(item.get("health") or "").lower():
            continue
        out.append(dict(item))
    out.sort(key=lambda item: str(item.get("runtime") or ""), reverse=True)
    return out


def render_training_actions(item: dict[str, object]) -> str:
    command = str(item.get("command") or "")
    actions: list[str] = []
    if command:
        actions.append(f"<button class='small' type='button' data-copy='{esc(command)}'>Copy command</button>")
    if item.get("pid") not in {None, "", "-"} and item.get("kind") == "process":
        proc = item.get("process") if isinstance(item.get("process"), dict) else item
        actions.append(f"<button class='small' type='button' data-copy='{esc(debug_context_message(proc, server_alias=item.get('host')))}'>Context</button>")
        actions.append(f"<button class='small' type='button' data-copy='{esc(process_adopt_command(proc))}'>Copy adopt</button>")
    if item.get("name") and item.get("kind") == "run":
        name = str(item.get("name"))
        actions.append(f"<button class='small' type='button' data-copy='labgpu logs {esc(name)} --tail 100'>Tail log</button>")
        actions.append(f"<button class='small' type='button' data-copy='labgpu diagnose {esc(name)}'>Diagnose</button>")
        actions.append(f"<button class='small' type='button' data-copy='labgpu context {esc(name)} --copy'>Context</button>")
    return " ".join(actions) or "-"


def health_badge(value: object) -> str:
    text = str(value or "").lower()
    if any(word in text for word in ("error", "failed", "critical", "zombie", "oom", "traceback")):
        return "error"
    if any(word in text for word in ("warning", "suspected", "idle", "busy", "io_wait", "disk")):
        return "warning"
    return "ok"


def render_available_gpus(
    items: object,
    ui: dict[str, object] | None = None,
    *,
    title: str = "Train Now / Recommended GPUs",
    limit: int | None = None,
    view_all: str | None = None,
    counts: dict[str, object] | None = None,
) -> str:
    items = filter_available_gpu_items(items, ui or {})
    if limit is not None:
        items = items[:limit]
    head = section_head(title, view_all)
    if not isinstance(items, list) or not items:
        busy = int((counts or {}).get("busy_gpus") or 0)
        idle = int((counts or {}).get("suspected_idle_gpus") or 0)
        total = int((counts or {}).get("total_gpus") or 0)
        ui_state = ui or {}
        return (
            f"<section class='panel'>{head}"
            "<p><strong>No clearly free GPU found.</strong></p>"
            f"<p class='muted'>{esc(busy)} GPUs are busy.</p>"
            f"<p class='muted'>{esc(idle)} GPUs look idle but occupied.</p>"
            "<div class='actions empty-actions'>"
            f"<a class='button' href='{esc(page_url('/gpus', ui_state, availability='busy'))}'>View busy GPUs</a>"
            f"<a class='button' href='{esc(page_url('/gpus', ui_state, availability='idle'))}'>View suspected idle GPUs</a>"
            f"<a class='button' href='{esc(page_url('/gpus', ui_state, availability='all'))}'>View all GPUs ({esc(total)})</a>"
            "</div></section>"
        )
    rows = "".join(
        f"<tr><td><a href='/servers/{esc(item.get('server'))}'>{esc(item.get('server'))}</a></td><td>GPU {esc(item.get('gpu_index'))}</td><td>{esc(short(item.get('name') or '', 34))}</td><td>{esc(item.get('memory_free_mb'))} MB</td><td>{esc(item.get('utilization_gpu'))}%</td><td>{esc(item.get('temperature'))} C</td><td>{esc(item.get('disk_health'))}</td><td><code>{esc(item.get('ssh_command'))}</code><br><code>CUDA_VISIBLE_DEVICES={esc(item.get('cuda_visible_devices'))}</code><br>{render_open_ssh_button(item.get('server'), item.get('gpu_index'))}</td></tr>"
        for item in items
        if isinstance(item, dict)
    )
    return f"<section class='panel'>{head}<table><tr><th>Server</th><th>GPU</th><th>Model</th><th>Free memory</th><th>Util</th><th>Temp</th><th>Disk</th><th>Copy</th></tr>{rows}</table></section>"


def render_gpu_finder(overview: dict[str, object], ui: dict[str, object]) -> str:
    items = filter_gpu_items(overview.get("gpu_items") or [], ui)
    if not items:
        return render_available_gpus([], ui, title="Train Now / Recommended GPUs", counts=overview)
    cards = "".join(render_gpu_recommendation_card(item) for item in items)
    return f"<section class='panel'><div class='section-head'><h2>Train Now Recommendations</h2><a href='{esc(page_url('/gpus', ui, availability='all'))}'>View all</a></div><div class='gpu-list'>{cards}</div></section>"


def render_gpu_recommendation_card(item: dict[str, object]) -> str:
    rec = gpu_recommendation(item)
    reasons = ranking.gpu_recommendation_reasons(item, rec)
    reason_items = "".join(f"<li>{esc(reason)}</li>" for reason in reasons)
    memory_free = format_memory(item.get("memory_free_mb"))
    memory_total = format_memory(item.get("memory_total_mb"))
    ssh_command = str(item.get("ssh_command") or ("ssh " + str(item.get("server") or "")))
    cuda = str(item.get("cuda_visible_devices") or item.get("index") or "")
    snippet = ranking.launch_snippet(item)
    open_terminal = render_open_ssh_button(item.get("server"), item.get("index"))
    availability = str(item.get("availability") or item.get("status") or "unknown")
    availability_label = "GPU free" if availability in {"free", "probably_available"} else "GPU busy" if availability == "busy" else availability
    availability_class = "ok" if availability in {"free", "probably_available"} else "warning" if availability == "busy" else ""
    return f"""
    <article class="gpu-choice {esc(rec['class'])}" data-gpu-choice="1" data-model="{esc(item.get('name') or '')}" data-free-mb="{esc(item.get('memory_free_mb') or 0)}" data-tags="{esc(join_values(item.get('tags') or item.get('server_tags') or []))}" data-server="{esc(item.get('server') or '')}" data-gpu-index="{esc(item.get('index'))}">
      <div class="card-head">
        <div>
          <h3><a href="/servers/{esc(item.get('server'))}">{esc(item.get('server'))}</a> · GPU {esc(item.get('index'))}</h3>
          <p>{esc(short(item.get('name') or '', 42))}</p>
        </div>
        <span class="badge {esc(rec['severity'])}">{esc(rec['label'])}</span>
      </div>
      <p class="warn-text">{esc(rec['reason'])}</p>
      <div class="meta">
        <span class="badge {esc(availability_class)}">{esc(availability_label)}</span>
        <span><span>Free memory</span> {esc(memory_free)} / {esc(memory_total)}</span>
        <span><span>GPU util</span> {esc(item.get('utilization_gpu'))}%</span>
        <span><span>Temp</span> {esc(item.get('temperature'))} C</span>
        <span><span>Load</span> {esc(load_value(item.get('load')))}</span>
        <span><span>Choice score</span> {esc(rec['score'])}</span>
      </div>
      <div class="meta">
        <code>{esc(ssh_command)}</code>
        <code>CUDA_VISIBLE_DEVICES={esc(cuda)}</code>
      </div>
      <div class="actions">
        <button class="small" type="button" data-copy="{esc(ssh_command)}">Copy SSH command</button>
        <button class="small" type="button" data-copy="CUDA_VISIBLE_DEVICES={esc(cuda)}">Copy CUDA_VISIBLE_DEVICES</button>
        <button class="small" type="button" data-copy="{esc(snippet)}">Copy launch snippet</button>
        {open_terminal}
      </div>
      <details>
        <summary>Why recommended</summary>
        <ul>{reason_items}</ul>
      </details>
    </article>
    """


def render_open_ssh_button(server: object, gpu_index: object | None = None) -> str:
    if not ServerHandler.action_allowed or ServerHandler.fake_lab or not server:
        return ""
    gpu_attr = f' data-gpu-index="{esc(gpu_index)}"' if gpu_index not in {None, ""} else ""
    return f'<button class="small" type="button" data-open-ssh="{esc(server)}"{gpu_attr}>Enter Server</button>'


def filter_available_gpu_items(items: object, ui: dict[str, object]) -> list[dict[str, object]]:
    return ranking.filter_available_gpu_items(items, ui)


def filter_gpu_items(items: object, ui: dict[str, object]) -> list[dict[str, object]]:
    return ranking.filter_gpu_items(items, ui)


def filter_process_items(items: object, ui: dict[str, object]) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    q = str(ui.get("q") or "").lower()
    server = str(ui.get("server") or "").lower()
    health = str(ui.get("health") or "").lower()
    filtered: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        haystack = " ".join(
            [
                str(item.get("server") or ""),
                str(item.get("user") or ""),
                str(item.get("pid") or ""),
                str(item.get("command") or ""),
                str(item.get("health_status") or ""),
            ]
        ).lower()
        if q and q not in haystack:
            continue
        if server and server not in str(item.get("server") or "").lower():
            continue
        if health and health != str(item.get("health_status") or "").lower():
            continue
        filtered.append(item)
    filtered.sort(key=lambda item: int(item.get("runtime_seconds") or 0), reverse=True)
    return filtered


def filter_alert_items(items: object, ui: dict[str, object]) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    q = str(ui.get("q") or "").lower()
    severity = str(ui.get("severity") or "").lower()
    status = str(ui.get("alert_status") or "active").lower()
    filtered: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        haystack = " ".join([str(item.get("server") or ""), str(item.get("type") or ""), str(item.get("message") or "")]).lower()
        if q and q not in haystack:
            continue
        if severity and severity != str(item.get("severity") or "").lower():
            continue
        if status != "all" and status != str(item.get("status") or "active").lower():
            continue
        filtered.append(item)
    filtered.sort(key=lambda item: (alert_rank(item.get("severity")), str(item.get("server") or "")))
    return filtered


def filter_hosts(items: object, ui: dict[str, object]) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    q = str(ui.get("q") or "").lower()
    tag = str(ui.get("tag") or "").lower()
    online_only = bool(ui.get("online"))
    has_free = bool(ui.get("free"))
    has_alerts = bool(ui.get("alerts"))
    has_mine = bool(ui.get("mine"))
    filtered: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        haystack = " ".join(
            [
                str(item.get("alias") or ""),
                str(item.get("remote_hostname") or ""),
                str(item.get("group") or ""),
                join_values(item.get("tags") or []),
                " ".join(str(gpu.get("name") or "") for gpu in item.get("gpus") or [] if isinstance(gpu, dict)),
                top_users(item.get("processes") or []),
            ]
        ).lower()
        if q and q not in haystack:
            continue
        if tag and tag not in join_values(item.get("tags") or []).lower():
            continue
        if online_only and not item.get("online"):
            continue
        if has_free and not item.get("available_gpus"):
            continue
        if has_alerts and not item.get("alerts"):
            continue
        if has_mine and not item.get("my_processes"):
            continue
        filtered.append(item)
    filtered.sort(key=lambda item: (not bool(item.get("online")), str(item.get("alias") or "")))
    return filtered


def render_my_processes(
    items: object,
    *,
    ui: dict[str, object] | None = None,
    title: str = "My GPU Processes",
    limit: int | None = None,
    view_all: str | None = None,
) -> str:
    items = filter_process_items(items, ui or {})
    if limit is not None:
        items = items[:limit]
    head = section_head(title, view_all)
    if not isinstance(items, list) or not items:
        return f"<section class='panel'>{head}<p class='muted'>No GPU process owned by the SSH user.</p></section>"
    rows = "".join(
        render_process_row(
            item,
            include_gpu=True,
            server_alias=item.get("server"),
            show_server=True,
            action_allowed=ServerHandler.action_allowed,
        )
        for item in items
        if isinstance(item, dict)
    )
    return f"<section class='panel'>{head}<table><tr><th>Server</th><th>GPU</th><th>User</th><th>PID</th><th>Runtime</th><th>State</th><th>Memory</th><th>Health</th><th>Command</th><th>Action</th></tr>{rows}</table></section>"


def render_alerts(
    items: object,
    *,
    ui: dict[str, object] | None = None,
    title: str = "Alerts",
    limit: int | None = None,
    view_all: str | None = None,
) -> str:
    items = filter_alert_items(items, ui or {})
    if limit is not None:
        items = items[:limit]
    head = section_head(title, view_all)
    if not isinstance(items, list) or not items:
        return f"<section class='panel'>{head}<p class='muted'>No current alerts.</p></section>"
    rows = "".join(
        f"<tr><td><a href='/servers/{esc(item.get('server'))}'>{esc(item.get('server'))}</a></td><td>{esc(item.get('type') or '-')}</td><td><span class='badge {esc(item.get('severity'))}'>{esc(item.get('severity'))}</span></td><td>{esc(item.get('status') or 'active')}</td><td title='{esc(item.get('first_seen') or '')}'>{esc(relative_time(item.get('last_seen')))}</td><td>{esc(item.get('message'))}</td><td>{render_alert_action(item)}</td></tr>"
        for item in items
        if isinstance(item, dict)
    )
    return f"<section class='panel'>{head}<table><tr><th>Server</th><th>Type</th><th>Severity</th><th>Status</th><th>Last seen</th><th>Message</th><th>Action</th></tr>{rows}</table></section>"


def scoped_alert_items(data: dict[str, object], items: object) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    hosts = data.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        return [item for item in items if isinstance(item, dict)]
    aliases = {str(host.get("alias") or "") for host in hosts if isinstance(host, dict) and host.get("alias")}
    if not aliases:
        return [item for item in items if isinstance(item, dict)]
    return [item for item in items if isinstance(item, dict) and str(item.get("server") or "") in aliases]


def render_alert_action(item: dict[str, object]) -> str:
    key = item.get("key")
    if not key:
        return "-"
    status = str(item.get("status") or "active")
    if status == "active":
        return (
            f"<button class='small' data-alert-action='dismiss' data-alert-key='{esc(key)}' type='button'>Dismiss</button> "
            f"<button class='small' data-alert-action='snooze' data-alert-key='{esc(key)}' type='button'>Snooze</button>"
        )
    if status in {"dismissed", "snoozed"}:
        return f"<button class='small' data-alert-action='activate' data-alert-key='{esc(key)}' type='button'>Restore</button>"
    return "-"


def render_detail(data: dict[str, object]) -> str:
    hosts = data.get("hosts") or []
    host = hosts[0] if hosts else {}
    if not isinstance(host, dict):
        host = {}
    json_href = "/api/servers"
    if not host:
        content = "<p class='muted'>Server not found.</p>"
    else:
        host = display_with_cache(host)
        json_href = f"/api/servers/{esc(host.get('alias'))}"
        content = f"""
        <section class="toolbar">
          <div>
            <h1>{esc(host.get('alias'))}</h1>
            <p>{esc(host.get('remote_hostname') or host.get('hostname') or '')}</p>
          </div>
        </section>
        {render_cache_notice(host)}
        {render_health(host)}
        {render_labgpu_runs(host)}
        <section class="panel"><h2>Disks</h2>{render_disk_table(host.get('disks') or [])}</section>
        <section class="panel"><h2>GPUs</h2><div class="gpu-grid">{''.join(render_gpu_card(gpu, server_alias=host.get('alias')) for gpu in host.get('gpus') or [])}</div></section>
        <section class="panel"><h2>Processes</h2>{render_process_table(host.get('processes') or [], server_alias=host.get('alias'))}</section>
        """
    return page("LabGPU Server", content, status=render_data_status(data), json_href=json_href)


def render_host_card(host: object, *, compact: bool = False) -> str:
    if not isinstance(host, dict):
        return ""
    online = bool(host.get("online"))
    health = server_health(host)
    status = "online" if online else "offline"
    cached = host.get("cached") if isinstance(host.get("cached"), dict) else None
    using_cache = bool(cached and (not online or host.get("probe_incomplete")) and not (host.get("gpus") or []))
    if using_cache and online and host.get("probe_incomplete"):
        status_label = "online · cached"
    elif using_cache:
        status_label = "offline · cached"
    else:
        status_label = f"{status} · {health}" if online and health != "ok" else status
    error = host.get("error")
    gpus = host.get("gpus") or []
    display_host = cached if using_cache and cached else host
    display_gpus = gpus or (display_host.get("gpus") if isinstance(display_host, dict) else []) or []
    disk = host.get("disk") or {}
    if using_cache and isinstance(display_host, dict):
        disk = display_host.get("disk") or {}
    gpu_rows = "".join(render_gpu_row(gpu) for gpu in display_gpus) or "<tr><td colspan='5' class='muted'>No GPU data.</td></tr>"
    summary = gpu_summary(display_gpus)
    memory = display_host.get("memory") if isinstance(display_host, dict) and isinstance(display_host.get("memory"), dict) else {}
    mem = memory.get("mem") if isinstance(memory.get("mem"), dict) else {}
    mode = host.get("mode") or "offline"
    href = f"/servers/{esc(host.get('alias'))}"
    cache_prefix = "cached " if using_cache else ""
    cache_notice = ""
    if using_cache and online and host.get("probe_incomplete"):
        cache_notice = (
            f"<p class='muted' title='{esc(host.get('last_seen'))}'>SSH is reachable, but the live GPU refresh timed out. "
            f"Showing cached snapshot from {esc(relative_time(host.get('last_seen')))}.</p>"
        )
    elif using_cache:
        cache_notice = (
            f"<p class='muted' title='{esc(host.get('last_seen'))}'>Showing cached snapshot from "
            f"{esc(relative_time(host.get('last_seen')))} because the current SSH probe failed.</p>"
        )
    gpu_table = "" if compact else f"""
      <table>
        <tr><th>GPU</th><th>Name</th><th>Memory</th><th>Util</th><th>Processes</th></tr>
        {gpu_rows}
      </table>
    """
    alerts = host.get("alerts") or []
    probe_label = "refreshing" if host.get("cache_miss") else "cached" if host.get("from_cache") else f"probe {format_latency(host.get('elapsed_ms'))}"
    probe_class = "" if host.get("from_cache") else "warn-text" if probe_seconds(host.get("elapsed_ms")) >= 5 else ""
    return f"""
    <article class="card">
      <div class="card-head">
        <div>
          <h2><a href="{href}">{esc(host.get('alias'))}</a></h2>
          <p>{esc(host.get('remote_hostname') or host.get('hostname') or '')}</p>
        </div>
        <span class="pill {esc(status)} {esc(health)}">{esc(status_label)}</span>
      </div>
      <div class="meta">
        {f"<span>group {esc(host.get('group'))}</span>" if host.get('group') else ""}
        <span>user {esc(host.get('user') or '-')}</span>
        <span>port {esc(host.get('port') or '22')}</span>
        <span>{esc(mode)}</span>
        <span>{esc(join_values(host.get('tags') or []))}</span>
        <span class="{esc(probe_class)}">{esc(probe_label)}</span>
      </div>
      <div class="meta">
        <span>{esc(cache_prefix)}{esc(summary['total'])} GPUs</span>
        <span>{esc(cache_prefix)}{esc(summary['free'])} free / {esc(summary['busy'])} busy</span>
        <span>{esc(cache_prefix)}load {esc(load_label(display_host if isinstance(display_host, dict) else host))}</span>
        <span>{esc(cache_prefix)}mem {esc(mem.get('used_percent') if isinstance(mem, dict) else '-')}%</span>
        <span>{esc(cache_prefix)}disk {esc(disk.get('use_percent') if isinstance(disk, dict) else '-')}</span>
      </div>
      <div class="meta">
        <span>{esc(cache_prefix)}{esc(display_host.get('uptime') if isinstance(display_host, dict) else host.get('uptime') or '-')}</span>
        <span>{esc(cache_prefix)}top users {esc(top_users(display_host.get('processes') if isinstance(display_host, dict) else host.get('processes') or []))}</span>
        <span>alerts {esc(len(alerts) if isinstance(alerts, list) else 0)}</span>
        <span title="{esc(host.get('probed_at') or '-')}">last updated {esc(relative_time(host.get('probed_at')))}</span>
      </div>
      {cache_notice}
      {f"<p class='error'>{esc(error)}</p>" if error else ""}
      {gpu_table}
    </article>
    """


def render_cache_notice(host: dict[str, object]) -> str:
    if not isinstance(host.get("cached"), dict):
        return ""
    if host.get("online") and host.get("probe_incomplete"):
        return (
            "<section class='panel'>"
            "<div class='meta'><span class='badge warning'>Cached GPU snapshot</span>"
            f"<span title='{esc(host.get('last_seen'))}'>last live GPU data {esc(relative_time(host.get('last_seen')))}</span></div>"
            f"<p class='muted'>SSH is reachable, but the live GPU refresh timed out: {esc(host.get('error') or 'probe timeout')}. "
            "GPU, process, disk, and health details below are from the last successful probe.</p>"
            "</section>"
        )
    if host.get("online"):
        return ""
    return (
        "<section class='panel'>"
        "<div class='meta'><span class='badge warning'>Cached snapshot</span>"
        f"<span title='{esc(host.get('last_seen'))}'>last live data {esc(relative_time(host.get('last_seen')))}</span></div>"
        f"<p class='muted'>Current SSH probe failed: {esc(host.get('error') or 'unavailable')}. "
        "GPU, process, disk, and health details below are from the last successful probe.</p>"
        "</section>"
    )


def display_with_cache(host: dict[str, object]) -> dict[str, object]:
    cached = host.get("cached")
    if (host.get("online") and not host.get("probe_incomplete")) or not isinstance(cached, dict):
        return host
    merged = dict(cached)
    for key in (
        "alias",
        "hostname",
        "user",
        "port",
        "proxyjump",
        "online",
        "error",
        "elapsed_ms",
        "mode",
        "last_seen",
        "cached",
        "probe_status",
        "probe_incomplete",
    ):
        if key in host:
            merged[key] = host[key]
    return merged


def render_gpu_row(gpu: object) -> str:
    if not isinstance(gpu, dict):
        return ""
    processes = gpu.get("processes") or []
    process_text = "<br>".join(
        f"{esc(user_label(proc.get('user')))} pid {esc(proc.get('pid'))} {esc(format_memory(proc.get('used_memory_mb')))} {esc(short(proc.get('command') or '', 80))}"
        for proc in processes
        if isinstance(proc, dict)
    ) or "<span class='muted'>free</span>"
    return (
        f"<tr><td>{esc(gpu.get('index'))}</td>"
        f"<td>{esc(short(gpu.get('name') or '', 22))}</td>"
        f"<td>{esc(format_memory(gpu.get('memory_used_mb')))}/{esc(format_memory(gpu.get('memory_total_mb')))}</td>"
        f"<td>{esc(gpu.get('utilization_gpu'))}% / {esc(gpu.get('temperature'))} C</td>"
        f"<td>{process_text}</td></tr>"
    )


def render_health(host: dict[str, object]) -> str:
    memory = host.get("memory") if isinstance(host.get("memory"), dict) else {}
    mem = memory.get("mem") if isinstance(memory.get("mem"), dict) else {}
    swap = memory.get("swap") if isinstance(memory.get("swap"), dict) else {}
    summary = gpu_summary(host.get("gpus") or [])
    return f"""
    <section class="health">
      <div><strong>{esc(host.get('mode') or 'offline')}</strong><span>mode</span></div>
      <div><strong>{esc(summary['free'])}/{esc(summary['total'])}</strong><span>free GPUs</span></div>
      <div><strong>{esc(load_label(host))}</strong><span>load</span></div>
      <div><strong>{esc(mem.get('used_percent') if isinstance(mem, dict) else '-')}%</strong><span>memory</span></div>
      <div><strong>{esc(swap.get('used_percent') if isinstance(swap, dict) else '-')}%</strong><span>swap</span></div>
      <div><strong title="{esc(host.get('probed_at') or '-')}">{esc(relative_time(host.get('probed_at')))}</strong><span>last updated</span></div>
    </section>
    """


def render_disk_table(disks: object) -> str:
    if not isinstance(disks, list) or not disks:
        return "<p class='muted'>No disk data.</p>"
    rows = "".join(
        f"<tr><td>{esc(disk.get('mount'))}</td><td>{esc(disk.get('size'))}</td><td>{esc(disk.get('used'))}</td><td>{esc(disk.get('available'))}</td><td>{esc(disk.get('use_percent'))}</td></tr>"
        for disk in disks
        if isinstance(disk, dict)
    )
    return f"<table><tr><th>Mount</th><th>Size</th><th>Used</th><th>Avail</th><th>Use</th></tr>{rows}</table>"


def render_labgpu_runs(host: dict[str, object]) -> str:
    if not host.get("labgpu_available"):
        return "<section class='panel'><h2>LabGPU</h2><p class='muted'>Agentless mode. LabGPU is not available on this remote PATH.</p></section>"
    runs = host.get("labgpu_runs")
    if not isinstance(runs, list) or not runs:
        return "<section class='panel'><h2>LabGPU</h2><p class='muted'>Enhanced mode detected. No run list returned.</p></section>"
    rows = "".join(
        f"<tr><td>{esc(run.get('name'))}</td><td>{esc(run.get('status'))}</td><td>{esc(run.get('user'))}</td><td>{esc(join_values(run.get('requested_gpu_indices') or run.get('gpu_indices') or []))}</td><td>{esc(run.get('failure_reason') or '-')}</td></tr>"
        for run in runs
        if isinstance(run, dict)
    )
    return f"<section class='panel'><h2>LabGPU Runs</h2><table><tr><th>Name</th><th>Status</th><th>User</th><th>GPU</th><th>Reason</th></tr>{rows}</table></section>"


def render_gpu_card(gpu: object, *, server_alias: object | None = None) -> str:
    if not isinstance(gpu, dict):
        return ""
    processes = gpu.get("processes") or []
    rows = "".join(render_process_row(proc, include_gpu=False, server_alias=server_alias, action_allowed=ServerHandler.action_allowed) for proc in processes if isinstance(proc, dict))
    if not rows:
        rows = "<tr><td colspan='7' class='muted'>free</td></tr>"
    return f"""
    <article class="gpu-card">
      <h3>GPU {esc(gpu.get('index'))} <span>{esc(short(gpu.get('name') or '', 32))}</span></h3>
      <div class="meta">
        <span>{esc(format_memory(gpu.get('memory_used_mb')))}/{esc(format_memory(gpu.get('memory_total_mb')))}</span>
        <span>{esc(gpu.get('utilization_gpu'))}% util</span>
        <span>{esc(gpu.get('temperature'))} C</span>
        <span>{esc(gpu.get('availability') or gpu.get('status') or 'unknown')}</span>
        <span>{esc(gpu.get('confidence') or '')}</span>
      </div>
      {f"<p class='muted'>{esc(gpu.get('health_reason'))}</p>" if gpu.get('health_reason') else ""}
      <table><tr><th>User</th><th>PID</th><th>Runtime</th><th>Memory</th><th>Health</th><th>Command</th><th>Action</th></tr>{rows}</table>
    </article>
    """


def render_process_table(processes: object, *, server_alias: object | None = None) -> str:
    if not isinstance(processes, list) or not processes:
        return "<p class='muted'>No GPU compute processes.</p>"
    rows = "".join(
        render_process_row(proc, include_gpu=True, server_alias=server_alias, action_allowed=ServerHandler.action_allowed)
        for proc in processes
        if isinstance(proc, dict)
    )
    return f"<table><tr><th>GPU</th><th>User</th><th>PID</th><th>Runtime</th><th>State</th><th>Memory</th><th>Health</th><th>Command</th><th>Action</th></tr>{rows}</table>"


def render_process_row(
    proc: dict[str, object],
    *,
    include_gpu: bool,
    server_alias: object | None = None,
    show_server: bool = False,
    action_allowed: bool = False,
) -> str:
    full_command = str(proc.get("command") or "")
    command = short(full_command, 120)
    gpu_value = proc.get("gpu_index") if proc.get("gpu_index") is not None else short(proc.get("gpu_uuid") or "", 14)
    gpu = f"<td>{esc(gpu_value)}</td>" if include_gpu else ""
    server = f"<td>{esc(server_alias)}</td>" if show_server and server_alias is not None else ""
    state = f"<td title='{esc(proc.get('state') or '-')}'>{esc(process_state_label(proc.get('state')))}</td>" if include_gpu else ""
    health_class = proc.get("health_severity") or proc.get("health_status") or "unknown"
    confidence = proc.get("confidence")
    confidence_text = f" · {confidence}" if confidence else ""
    health_title = process_evidence_text(proc) or proc.get("health_reason") or ""
    health = f"<span class='badge {esc(health_class)}' title='{esc(health_title)}'>{esc(proc.get('health_status') or 'unknown')}{esc(confidence_text)}</span>"
    action = render_process_action(proc, server_alias=server_alias, action_allowed=action_allowed)
    command_html = (
        f"<code>{esc(command)}</code> "
        f"<button class='small' type='button' data-copy='{esc(full_command)}'>Copy</button>"
        f"<details><summary>Expand</summary><code>{esc(full_command)}</code></details>"
    )
    return (
        f"<tr>{server}{gpu}<td>{esc(user_label(proc.get('user')))}</td><td>{esc(proc.get('pid'))}</td>"
        f"<td>{esc(proc.get('runtime') or human_duration(proc.get('runtime_seconds')))}</td>{state}"
        f"<td>{esc(format_memory(proc.get('used_memory_mb')))}</td><td title='{esc(health_title)}'>{health}</td>"
        f"<td>{command_html}</td><td>{action}</td></tr>"
    )


def render_process_action(proc: dict[str, object], *, server_alias: object | None, action_allowed: bool) -> str:
    view = f"<a class='small' href='/servers/{esc(server_alias)}'>View</a> " if server_alias else ""
    context = (
        f"<button class='small' type='button' data-copy='{esc(debug_context_message(proc, server_alias=server_alias))}'>Context</button> "
        if server_alias
        else ""
    )
    if proc.get("actions_disabled_reason"):
        return f"{view}{context}<span class='muted'>{esc(proc.get('actions_disabled_reason'))}</span>"
    if proc.get("is_current_user") and action_allowed and server_alias:
        adopt = process_adopt_command(proc)
        return (
            f"{view}{context}"
            f"<button class='small' type='button' data-copy='{esc(adopt)}'>Copy adopt</button> "
            f"<button class='small danger' data-stop='term' data-server='{esc(server_alias)}' data-pid='{esc(proc.get('pid'))}' "
            f"data-gpu='{esc(proc.get('gpu_index') if proc.get('gpu_index') is not None else '-')}' "
            f"data-runtime='{esc(proc.get('runtime') or human_duration(proc.get('runtime_seconds')))}' "
            f"data-memory='{esc(format_memory(proc.get('used_memory_mb')))}' "
            f"data-user='{esc(proc.get('user') or '')}' data-start='{esc(proc.get('start_time') or '')}' "
            f"data-hash='{esc(proc.get('command_hash') or '')}' data-command='{esc(short(proc.get('command') or '', 180))}'>Stop process</button>"
        )
    if proc.get("is_current_user") and not action_allowed:
        return f"{view}{context}<span class='muted'>actions disabled</span>"
    owner = owner_message(proc, server_alias=server_alias)
    return (
        f"{view}{context}"
        f"<button class='small' type='button' data-copy='{esc(owner)}'>Copy owner message</button>"
    )


def process_adopt_command(proc: dict[str, object]) -> str:
    pid = proc.get("pid")
    gpu = proc.get("gpu_index")
    gpu_arg = f" --gpu {gpu}" if gpu is not None else ""
    return f"labgpu adopt {pid} --name NAME{gpu_arg}"


def owner_message(proc: dict[str, object], *, server_alias: object | None) -> str:
    gpu = proc.get("gpu_index") if proc.get("gpu_index") is not None else short(proc.get("gpu_uuid") or "-", 14)
    evidence = process_evidence_text(proc)
    pieces = [
        "Hi, quick check on this GPU process:",
        f"server={server_alias or '-'}",
        f"gpu={gpu}",
        f"pid={proc.get('pid')}",
        f"user={proc.get('user') or 'unknown'}",
        f"runtime={proc.get('runtime') or human_duration(proc.get('runtime_seconds'))}",
        f"gpu_mem={format_memory(proc.get('used_memory_mb'))}",
    ]
    if evidence:
        pieces.append(f"evidence={evidence}")
    pieces.append("Could you confirm whether it is still needed when you have a chance?")
    return " ".join(pieces)


def debug_context_message(proc: dict[str, object], *, server_alias: object | None) -> str:
    return "\n".join(
        [
            "# LabGPU Process Context",
            f"server: {server_alias or '-'}",
            f"gpu: {proc.get('gpu_index') if proc.get('gpu_index') is not None else proc.get('gpu_uuid') or '-'}",
            f"pid: {proc.get('pid')}",
            f"user: {proc.get('user') or 'unknown'}",
            f"runtime: {proc.get('runtime') or human_duration(proc.get('runtime_seconds'))}",
            f"gpu_memory: {format_memory(proc.get('used_memory_mb'))}",
            f"health: {proc.get('health_status') or 'unknown'}",
            f"evidence: {process_evidence_text(proc) or proc.get('health_reason') or '-'}",
            "",
            "command:",
            str(proc.get("command") or ""),
        ]
    )


def process_evidence_text(proc: dict[str, object]) -> str:
    evidence = proc.get("idle_evidence")
    if not isinstance(evidence, dict):
        return ""
    parts = []
    elapsed = evidence.get("elapsed_seconds")
    if elapsed is not None:
        parts.append(f"GPU util < 3% over {human_duration(int(elapsed))}")
    elif evidence.get("low_util_samples") is not None:
        parts.append(f"GPU util < 3% in {evidence.get('low_util_samples')} samples")
    if evidence.get("sample_count") is not None:
        parts.append(f"based on {evidence.get('sample_count')} samples")
    if evidence.get("vram_occupied_mb") is not None:
        parts.append(f"VRAM occupied {format_memory(evidence.get('vram_occupied_mb'))}")
    if proc.get("cpu_low_samples") is not None:
        parts.append(f"CPU low in {proc.get('cpu_low_samples')} samples")
    if evidence.get("confidence"):
        parts.append(f"confidence {evidence.get('confidence')}")
    return "; ".join(parts)


def page(title: str, body: str, *, status: str = "", json_href: str = "/api/servers") -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<script>
(function() {{
  try {{
    const saved = localStorage.getItem("labgpu-theme") || "system";
    const dark = saved === "dark" || (saved === "system" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  }} catch (error) {{
    document.documentElement.dataset.theme = "light";
  }}
}})();
</script>
<style>
:root{{
  color-scheme: light;
  --bg:#f7f7f4;
  --surface:#fff;
  --surface-soft:#fcfcfa;
  --border:#d8d8d0;
  --border-soft:#ecece6;
  --text:#1f2328;
  --muted:#667085;
  --link:#344054;
  --code:#1f2328;
  --button:#fff;
  --row:#eee;
  --badge:#eef2f6;
}}
html[data-theme="dark"]{{
  color-scheme: dark;
  --bg:#0f1419;
  --surface:#161b22;
  --surface-soft:#111820;
  --border:#2b3440;
  --border-soft:#25303b;
  --text:#e6edf3;
  --muted:#9aa7b7;
  --link:#d5dee9;
  --code:#d8e2ef;
  --button:#1d2630;
  --row:#27313d;
  --badge:#243244;
}}
body{{font:14px/1.45 system-ui,sans-serif;margin:0;background:var(--bg);color:var(--text)}}
main{{width:min(1280px,calc(100vw - 32px));margin:0 auto;padding:22px 0 36px}}
h1,h2,h3,p{{margin:0}} h1{{font-size:28px}} h2{{font-size:18px}} h3{{font-size:15px}} p,.muted{{color:var(--muted)}}
a{{color:inherit}} code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--code)}}
.topbar{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:24px}}
.topnav,.top-controls{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.top-controls{{justify-content:flex-end;max-width:min(560px,45vw)}}
.topnav a,.top-controls a,.top-controls button{{border:1px solid var(--border);background:var(--button);border-radius:999px;padding:6px 10px;text-decoration:none;color:var(--link);font:inherit;cursor:pointer;white-space:nowrap}}
.cache-status{{display:flex;gap:8px;align-items:center;justify-content:flex-end;flex:1 1 100%;min-width:min(420px,100%);color:var(--muted);font-size:12px}}
.cache-message{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:min(300px,24vw)}}
.json-control{{display:none!important}}
html.show-json .json-control{{display:inline-flex!important}}
dialog{{border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);max-width:min(560px,calc(100vw - 32px));padding:18px}}
dialog::backdrop{{background:rgba(0,0,0,.45)}}
dialog label{{display:flex;flex-direction:column;gap:4px;margin:10px 0;color:var(--muted);font-size:12px}}
dialog select,dialog input{{border:1px solid var(--border);border-radius:6px;padding:8px;background:var(--button);color:var(--text);font:inherit}}
dialog fieldset{{border:1px solid var(--border-soft);border-radius:8px;margin:12px 0;padding:10px 12px}}
dialog legend{{color:var(--text);font-weight:700;padding:0 4px}}
dialog fieldset label{{flex-direction:row;align-items:center;color:var(--text);font-size:13px;margin:7px 0}}
.modal-actions{{display:flex;gap:8px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap}}
.toolbar{{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:18px}}
.actions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.button{{border:1px solid var(--border);background:var(--button);border-radius:6px;padding:7px 10px;color:var(--text);text-decoration:none;cursor:pointer}}
.groupbar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:-4px 0 14px}}
.group-chip{{border:1px solid var(--border);background:var(--button);border-radius:999px;padding:5px 10px;text-decoration:none;color:var(--link)}}
.group-chip.active{{border-color:#75c793;background:#ecfdf3;color:#067647}}
.group-actions{{display:grid;grid-template-columns:minmax(260px,.8fr) minmax(560px,1.8fr);gap:14px;align-items:start}}
.group-side{{display:grid;gap:14px}}
.filters{{display:flex;gap:10px;align-items:end;flex-wrap:wrap;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:14px}}
.filters label{{display:flex;flex-direction:column;gap:4px;color:var(--muted);font-size:12px}}
.filters input,.filters select{{border:1px solid var(--border);border-radius:6px;padding:7px 8px;min-width:150px;background:var(--button);color:var(--text)}}
.inline-setting{{display:flex;gap:8px;align-items:center;margin-top:8px}}
details{{margin-top:10px}}
summary{{cursor:pointer;color:var(--link);font-weight:600}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}}
.grid.compact{{grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}}
.split{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px;align-items:start}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;overflow:hidden}}
.card-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px}}
.meta{{display:flex;flex-wrap:wrap;gap:10px;margin:6px 0;color:var(--muted);font-size:13px}}
.pill{{border-radius:999px;padding:2px 9px;font-size:12px;background:#eee}}
.pill.online{{color:#067647;background:#ecfdf3}} .pill.offline{{color:#b42318;background:#fef3f2}}
.badge{{border-radius:999px;padding:2px 7px;font-size:12px;background:var(--badge);color:var(--link)}}
.badge.ok{{background:#ecfdf3;color:#067647}} .badge.warning{{background:#fffaeb;color:#b54708}} .badge.error{{background:#fef3f2;color:#b42318}}
.error{{color:#b42318;margin:8px 0}}
.small{{border:1px solid var(--border);border-radius:5px;background:var(--button);color:var(--text);padding:3px 7px;font-size:12px;cursor:pointer}}
.danger{{color:#b42318;border-color:#fda29b}} .danger-strong{{color:#fff;background:#b42318;border-color:#b42318}}
.panel{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;margin:14px 0;overflow:hidden}}
.assistant-panel{{display:grid;gap:12px}}
.assistant-examples{{display:flex;gap:8px;flex-wrap:wrap}}
.assistant-chat{{display:grid;gap:10px;max-height:440px;overflow:auto;border:1px solid var(--border-soft);border-radius:8px;padding:10px;background:var(--surface-soft)}}
.assistant-message{{white-space:pre-wrap;border:1px solid var(--border-soft);border-radius:8px;padding:10px;background:var(--surface)}}
.assistant-message-user{{justify-self:end;max-width:min(720px,90%);background:var(--button)}}
.assistant-message-assistant,.assistant-message-system{{justify-self:start;max-width:min(820px,94%)}}
.assistant-form{{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end}}
.assistant-form textarea{{border:1px solid var(--border);border-radius:8px;padding:10px;background:var(--button);color:var(--text);font:inherit;resize:vertical}}
.section-head{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:4px}}
.section-head a{{font-size:13px;color:var(--link)}}
.health{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}}
.health>div{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px}}
.health strong{{display:block;font-size:18px}} .health span{{color:var(--muted);font-size:12px}}
.summary-card.ok{{border-color:#75c793}} .summary-card.warning{{border-color:#f59e0b}} .summary-card.error{{border-color:#ef4444}}
.gpu-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px;margin-top:10px}}
.gpu-card{{border:1px solid var(--border-soft);border-radius:8px;padding:12px;background:var(--surface-soft);overflow:hidden}}
.gpu-card h3 span{{color:var(--muted);font-weight:500}}
.gpu-list{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;margin-top:12px}}
.gpu-choice{{border:1px solid var(--border-soft);border-radius:8px;padding:12px;background:var(--surface-soft)}}
.gpu-choice.recommended{{border-color:#75c793}} .gpu-choice.not-recommended{{border-color:#ef4444}} .gpu-choice.busy{{opacity:.86}}
.warn-text{{color:#b54708}}
.empty-actions{{margin-top:12px}}
table{{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}}
th,td{{border-top:1px solid var(--row);padding:7px;text-align:left;vertical-align:top}} th{{color:var(--muted)}}
html[data-theme="dark"] .pill.online{{color:#86efac;background:#143421}} html[data-theme="dark"] .pill.offline{{color:#fca5a5;background:#3a1717}}
html[data-theme="dark"] .badge.ok{{background:#143421;color:#86efac}} html[data-theme="dark"] .badge.warning{{background:#3b2a0a;color:#facc15}} html[data-theme="dark"] .badge.error{{background:#3a1717;color:#fca5a5}}
html[data-theme="dark"] .group-chip.active{{background:#143421;color:#86efac;border-color:#75c793}}
html[data-theme="dark"] .warn-text{{color:#facc15}}
html[data-theme="dark"] .danger{{color:#fca5a5;border-color:#7f1d1d}}
@media(max-width:980px){{.group-actions{{grid-template-columns:1fr}}}}
@media(max-width:860px){{.topbar{{flex-direction:column}}.top-controls{{justify-content:flex-start;max-width:none}}.cache-message{{max-width:calc(100vw - 96px)}}}}
@media(max-width:640px){{main{{width:calc(100vw - 20px)}}.grid,.split{{grid-template-columns:1fr}}.toolbar{{align-items:flex-start;flex-direction:column}}.assistant-form{{grid-template-columns:1fr}}}}
</style></head><body><main>{render_nav(status=status, json_href=json_href)}{body}</main>
<dialog id="stop-modal">
  <h2>Stop process?</h2>
  <p class="muted">This stops the single PID shown below. Child processes may continue unless this is a LabGPU-tracked run.</p>
  <table>
    <tr><th>Server</th><td id="modal-server"></td></tr>
    <tr><th>GPU</th><td id="modal-gpu"></td></tr>
    <tr><th>PID</th><td id="modal-pid"></td></tr>
    <tr><th>User</th><td id="modal-user"></td></tr>
    <tr><th>Runtime</th><td id="modal-runtime"></td></tr>
    <tr><th>Memory</th><td id="modal-memory"></td></tr>
    <tr><th>Command</th><td><code id="modal-command"></code></td></tr>
  </table>
  <p id="modal-result" class="muted"></p>
  <div class="modal-actions">
    <button class="button" id="modal-cancel" type="button">Cancel</button>
    <button class="button danger" id="modal-stop" type="button">Stop process</button>
    <button class="button danger-strong" id="modal-force" type="button" hidden>Force process kill</button>
  </div>
</dialog>
<dialog id="ssh-modal">
  <h2>Enter Server</h2>
  <p class="muted">Open a server shell with a local AI provider tunnel. Alpha supports Claude Code through the current CC Switch provider.</p>
  <table>
    <tr><th>Server</th><td id="ssh-modal-server"></td></tr>
    <tr><th>GPU</th><td><select id="ssh-gpu"><option value="">none</option></select></td></tr>
    <tr><th>Working directory</th><td>
      <input id="ssh-remote-cwd" list="ssh-remote-cwd-options" placeholder="/data/lsg/work/project" autocomplete="off">
      <datalist id="ssh-remote-cwd-options"></datalist>
      <p class="muted" id="ssh-folder-summary">VS Code Remote-SSH recent folders will appear here when available.</p>
    </td></tr>
  </table>
  <input type="hidden" id="ssh-proxy" value="ccswitch">
  <fieldset>
    <legend>AI App</legend>
    <label><input type="radio" name="ssh-agent" value="claude" checked> Claude Code</label>
    <label><input type="radio" name="ssh-agent" value="codex" disabled> Codex <span class="muted">coming soon</span></label>
    <label><input type="radio" name="ssh-agent" value="gemini" disabled> Gemini <span class="muted">coming soon</span></label>
  </fieldset>
  <fieldset>
    <legend>Mode</legend>
    <label><input type="radio" name="ssh-ai-mode" value="proxy_tunnel" checked> Proxy Tunnel <span class="muted">recommended, secrets stay local</span></label>
    <label><input type="radio" name="ssh-ai-mode" value="remote_write" disabled> Remote Write <span class="muted">advanced, coming soon</span></label>
  </fieldset>
  <p class="muted" id="ssh-provider-summary">Using current CC Switch Claude provider: -</p>
  <p class="muted" id="ssh-proxy-summary">Proxy tunnel: -</p>
  <p class="muted" id="ssh-ccswitch-status">Checking CC Switch...</p>
  <p class="muted" id="ssh-modal-hint">This flow exports <code>ANTHROPIC_BASE_URL</code> to a per-session gateway tunnel and uses a temporary <code>ANTHROPIC_API_KEY</code> session token. Real provider keys stay local. If SSH exits with remote forwarding failure, the selected remote gateway port may already be in use on that server.</p>
  <p id="ssh-modal-result" class="muted"></p>
  <div class="modal-actions">
    <button class="button" id="ssh-modal-cancel" type="button">Cancel</button>
    <button class="button" id="ssh-modal-open" type="button">Open Terminal</button>
  </div>
</dialog>
<script>
let paused = false;
const actionToken = "{esc(ServerHandler.action_token)}";
let selectedStopButton = null;
let selectedSshButton = null;
let ccswitchSummary = null;
let vscodeRecentFolders = [];
const themeButton = document.getElementById("theme-toggle");
const languageButton = document.getElementById("language-toggle");
const jsonToggle = document.getElementById("show-json-toggle");
const translations = {{
  "Overview": "总览",
  "Train Now": "现在开跑",
  "My Training": "我的训练",
  "Assistant": "助手",
  "Servers": "服务器",
  "Groups": "分组",
  "Alerts": "告警",
  "Settings": "设置",
  "JSON": "JSON",
  "Pause refresh": "暂停刷新",
  "Resume refresh": "继续刷新",
  "Refresh now": "立即刷新",
  "Cached page": "缓存页面",
  "Servers missing cache": "缺少缓存的服务器",
  "Cached data age": "缓存距上次刷新",
  "Scoped": "范围固定",
  "Interface": "界面",
  "Show JSON/API links": "显示 JSON/API 链接",
  "Show raw JSON/API links in the top-right controls. Most users can leave this off.": "在右上角控制区显示原始 JSON/API 链接。大多数用户可以保持关闭。",
  "Add Server": "新增服务器",
  "Add server": "新增服务器",
  "Add a new SSH GPU server for first-time setup. LabGPU can append a Host block to": "为首次配置新增一台 SSH GPU 服务器。LabGPU 可以追加一个 Host 配置到",
  "and save the server to": "并把服务器保存到",
  "If the SSH config file already exists, LabGPU writes a backup before appending. Existing SSH aliases are not overwritten.": "如果 SSH config 已存在，LabGPU 会先写备份再追加。已有 SSH alias 不会被覆盖。",
  "Dark": "深色",
  "Light": "浅色",
  "LabGPU Home": "LabGPU 主页",
  "LabGPU Assistant": "LabGPU 助手",
  "Assistant API": "助手 API",
  "Optional BYO OpenAI-compatible API": "可选：使用你自己的 OpenAI-compatible API",
  "Use my API for answers": "使用我的 API 回答",
  "API URL": "API 地址",
  "API key": "API key",
  "Remember key in this browser": "在这个浏览器记住 key",
  "Default mode uses local LabGPU rules only. API mode sends redacted workspace context to your configured endpoint and stays read-only/copy-only. LabGPU does not save this key to": "默认模式只使用本地 LabGPU 规则。API 模式会把脱敏后的工作台上下文发送到你配置的 endpoint，并且仍然只读、只生成可复制计划。LabGPU 不会把这个 key 保存到",
  "Ask LabGPU": "询问 LabGPU",
  "Find a GPU": "找 GPU",
  "Where are my jobs?": "任务在哪",
  "Explain failures": "解释失败",
  "Debug context": "调试上下文",
  "Chat with your GPU workspace. Read-only and copy-only in this alpha.": "和你的 GPU 工作台对话。Alpha 阶段只读、只生成可复制计划。",
  "Personal GPU workspace for students using shared SSH servers.": "给学生使用共享 SSH GPU 服务器的个人训练工作台。",
  "online servers": "在线服务器",
  "available GPUs": "可用 GPU",
  "my training processes": "我的训练进程",
  "Train Now / Recommended GPUs": "现在开跑 / 推荐 GPU",
  "Train Now Recommendations": "现在开跑推荐",
  "Copy an SSH command, CUDA_VISIBLE_DEVICES value, or LabGPU launch snippet.": "复制 SSH 命令、CUDA_VISIBLE_DEVICES 或 LabGPU 启动片段。",
  "My Runs": "我的任务",
  "Failed or Suspicious Runs": "失败或可疑任务",
  "No failed run, suspected idle process, or failure signal found.": "没有发现失败任务、疑似空转进程或失败信号。",
  "Problems": "问题",
  "Resource details for your SSH hosts.": "你的 SSH 主机资源详情。",
  "Choose home servers": "选择首页服务器",
  "Showing your saved enabled servers. Change this list in Settings.": "正在显示你保存并启用的服务器。可在设置中修改。",
  "Showing built-in demo servers.": "正在显示内置演示服务器。",
  "Showing SSH hosts from your config. Save selected hosts in Settings to make the home page faster.": "正在显示 SSH 配置中的主机。到设置保存常用服务器后，首页会更快。",
  "Showing hosts fixed by this UI launch. Settings are saved, but this view will stay scoped until you restart without --hosts.": "当前视图被本次 UI 启动参数固定。设置已经保存，但要不带 --hosts 重启 UI 后才会生效。",
  "View all": "查看全部",
  "My training": "我的训练",
  "Rank GPUs across SSH hosts by GPU availability, free VRAM, model, load, and tags.": "按 GPU 是否空闲、空闲显存、型号、负载和标签对 SSH 主机上的 GPU 排序。",
  "Your LabGPU runs, adopted runs, and agentless GPU processes across SSH servers.": "你在各 SSH 服务器上的 LabGPU 任务、接管任务和无代理 GPU 进程。",
  "Configured SSH GPU servers, health, disks, and free/busy GPUs.": "已配置的 SSH GPU 服务器、健康状态、磁盘和 GPU 忙闲。",
  "Disk, SSH, GPU, and process conditions that need attention.": "需要关注的磁盘、SSH、GPU 和进程状态。",
  "Saved Servers": "已保存服务器",
  "These enabled servers are shown on LabGPU Home by default.": "这些启用的服务器会默认显示在 LabGPU Home。",
  "Server Groups": "服务器分组",
  "Existing Groups": "已有分组",
  "Create Group": "创建分组",
  "Update Group Members": "更新分组成员",
  "Create or Update Group": "创建或更新分组",
  "Create a group name, select existing saved servers, and switch views by group when you train.": "创建分组名，勾选已保存的服务器，训练时就可以按分组切换视图。",
  "Create a group name first, even if you do not want to add servers yet.": "先创建一个分组名，即使现在还不想加入服务器也可以。",
  "Choose an existing group or Ungrouped, then select saved servers to move there.": "选择一个已有分组或未分组，然后勾选已保存服务器移动进去。",
  "Target group": "目标分组",
  "Move selected servers": "移动选中的服务器",
  "Move selected servers to this group": "把选中服务器移到这个分组",
  "Create group": "创建分组",
  "Example: create ": "例如：创建 ",
  "Add servers": "添加服务器",
  "Manage groups": "管理分组",
  "No groups yet.": "还没有分组。",
  "Create group / Assign selected servers": "创建分组 / 加入选中服务器",
  "You can create an empty group first. If you select servers, they join the named group. Leave the group name blank to remove selected servers from their group.": "你可以先创建一个空分组。如果选中服务器，它们会加入这个分组。分组名留空可以把选中的服务器移出当前分组。",
  "Delete Group Names": "删除分组名",
  "Deleting a group name does not delete servers. Servers in that group are moved back to ungrouped.": "删除分组名不会删除服务器。属于该分组的服务器会回到未分组。",
  "Delete selected group names": "删除选中的分组名",
  "No groups to delete.": "没有可删除的分组。",
  "Members": "成员",
  "Create groups after servers are saved. Groups let you switch Home, Train Now, My Training, Servers, Alerts, and Assistant between all servers and a custom pool.": "服务器保存后再创建分组。分组可以让 Home、现在开跑、我的训练、服务器、告警和助手在全部服务器与自定义资源池之间切换。",
  "Groups are optional. Create a group by typing the same group name on the servers you want together, then use the group chips on Home, Train Now, My Training, Servers, Alerts, or Assistant.": "分组是可选的。给想放在一起的服务器填写同一个分组名，就可以在主页、现在开跑、我的训练、服务器、告警或助手页面用分组按钮查看。",
  "Groups are optional. Type a group name, select existing saved servers, and save. Use groups when you want to view only AlphaLab, off-campus, H800, or any custom server set.": "分组是可选的。输入分组名，勾选已经保存的服务器，然后保存。需要只看 AlphaLab、校外服务器、H800 或其他自定义服务器集合时使用分组。",
  "Group name": "分组名",
  "Save group": "保存分组",
  "Leave the group name blank to remove the selected servers from their group.": "分组名留空可以把选中的服务器移出当前分组。",
  "Select": "选择",
  "Current group": "当前分组",
  "Save servers first, then create groups here.": "先保存服务器，然后在这里创建分组。",
  "Save groups": "保存分组",
  "Choose which SSH GPU servers appear in LabGPU Home.": "选择哪些 SSH GPU 服务器显示在 LabGPU Home。",
  "Server group": "服务器分组",
  "Group": "分组",
  "Ungrouped": "未分组",
  "All": "全部",
  "Import From SSH Config": "从 SSH 配置导入",
  "Import SSH hosts and manage the server inventory stored in": "导入 SSH 主机并管理保存在",
  "Select SSH aliases, set defaults, and save them into": "选择 SSH 别名、设置默认值，并保存到",
  "Select the SSH aliases you want on LabGPU Home. Saved enabled hosts are the default probe set, so choosing fewer servers makes the home page faster. Test connection opens a probe page that auto-detects GPU model, disks, load, and LabGPU Enhanced Mode when available.": "选择你想放在 LabGPU 首页的 SSH 别名。保存并启用的服务器会作为默认探测范围，所以少选一些会让首页更快。测试连接会打开探测页面，自动检测 GPU 型号、磁盘、负载和可用的 LabGPU Enhanced Mode。",
  "Search": "搜索",
  "Model": "型号",
  "Min free GB": "最小空闲 GB",
  "Server tag": "服务器标签",
  "Tag": "标签",
  "Sort": "排序",
  "Recommended": "推荐",
  "GPU free": "GPU 空闲",
  "GPU busy": "GPU 忙碌",
  "Choice score": "选择分",
  "A compute process is using this GPU.": "有计算进程正在使用这张 GPU。",
  "GPU memory is occupied with low current utilization.": "GPU 显存被占用，但当前利用率很低。",
  "GPU is free, but the server load is high.": "GPU 空闲，但服务器负载较高。",
  "High free memory for training.": "空闲显存充足，适合训练。",
  "GPU is free for training.": "GPU 空闲，可以训练。",
  "Why recommended": "推荐原因",
  "Free memory": "空闲显存",
  "GPU util": "GPU 利用率",
  "Temp": "温度",
  "Load": "负载",
  "Server load": "服务器负载",
  "Filter": "过滤",
  "Clear": "清除",
  "Notify me when GPU is free": "GPU 空闲时通知我",
  "Browser notification only": "仅浏览器通知",
  "Notify me": "通知我",
  "Clear watch": "清除监听",
  "When a matching GPU appears after refresh, the browser notification says: LabGPU - alpha_shi GPU 0 is available.": "刷新后如果出现匹配的 GPU，浏览器通知会显示：LabGPU - alpha_shi GPU 0 is available。",
  "No browser watch configured.": "未配置浏览器监听。",
  "Any": "任意",
  "Healthy": "健康",
  "Suspected idle": "疑似空转",
  "IO wait": "IO 等待",
  "Zombie": "僵尸进程",
  "Online only": "仅在线",
  "Has free GPU": "有空闲 GPU",
  "Has alerts": "有告警",
  "Has my processes": "有我的进程",
  "Critical": "严重",
  "Warning": "警告",
  "Info": "信息",
  "Active": "活跃",
  "Dismissed": "已忽略",
  "Resolved": "已解决",
  "All": "全部",
  "Name": "名称",
  "Host": "主机",
  "PID": "PID",
  "Runtime": "运行时长",
  "Last log": "最后日志",
  "Status": "状态",
  "Health": "健康",
  "Diagnosis": "诊断",
  "Action": "操作",
  "Source": "来源",
  "Signal": "信号",
  "Type": "类型",
  "Severity": "级别",
  "Last seen": "最后出现",
  "Message": "消息",
  "Alias": "别名",
  "Enabled": "启用",
  "Tags": "标签",
  "Disk paths": "磁盘路径",
  "Shared account": "共享账号",
  "Stop own process": "停止自己的进程",
  "HostName": "主机名",
  "User": "用户",
  "Port": "端口",
  "Probe": "探测",
  "Test connection": "测试连接",
  "Save selected hosts": "保存选中主机",
  "Write to SSH config": "写入 SSH config",
  "HostName or IP": "主机名或 IP",
  "SSH user": "SSH 用户",
  "Advanced SSH options": "高级 SSH 选项",
  "ProxyJump": "ProxyJump",
  "IdentityFile": "IdentityFile",
  "Copy SSH command": "复制 SSH 命令",
  "Copy CUDA_VISIBLE_DEVICES": "复制 CUDA_VISIBLE_DEVICES",
  "Copy launch snippet": "复制启动片段",
  "Open SSH terminal": "打开 SSH 终端",
  "Opening terminal...": "正在打开终端...",
  "Terminal opened": "终端已打开",
  "Choose a local proxy tunnel if you need one, then optionally start Codex, Claude Code, Gemini, or OpenClaw in the SSH terminal.": "如果需要可以选择本地代理隧道，然后可选在 SSH 终端里直接启动 Codex、Claude Code、Gemini 或 OpenClaw。",
  "Choose a local proxy if you need one, then optionally start Codex, Claude Code, Gemini, or OpenClaw in the SSH terminal.": "如果需要可以选择本地代理，然后可选在 SSH 终端里直接启动 Codex、Claude Code、Gemini 或 OpenClaw。",
  "Proxy tunnel": "代理隧道",
  "Local proxy": "本机代理",
  "No proxy": "不使用代理",
  "No proxy (server network)": "不使用代理（服务器自己的网络）",
  "CC Switch proxy": "CC Switch 代理",
  "Reverse local port 7890": "反向转发本地 7890",
  "Reverse local port 33210": "反向转发本地 33210",
  "Custom local port": "自定义本地端口",
  "Use local port 7890": "使用本机 7890",
  "Use local port 33210": "使用本机 33210",
  "Custom local proxy port": "自定义本机代理端口",
  "Remote tunnel port": "远端隧道端口（留空自动）",
  "Open after SSH": "SSH 后打开",
  "CC Switch provider": "CC Switch 供应商",
  "Agent provider": "Agent 供应商",
  "Use current local selection": "使用本机当前选择",
  "Shell only": "只打开 Shell",
  "Codex CLI": "Codex CLI",
  "Claude Code": "Claude Code",
  "Gemini CLI": "Gemini CLI",
  "OpenClaw Agent": "OpenClaw Agent",
  "Checking CC Switch...": "正在检查 CC Switch...",
  "Opening from a GPU card does not set ": "从 GPU 卡片打开不会设置 ",
  ". It only chooses the server to SSH into.": "。它只用来选择要 SSH 进去的服务器。",
  "Checking...": "检查中...",
  "Copy command": "复制命令",
  "Copy adopt": "复制接管命令",
  "Copy owner message": "复制询问消息",
  "Tail log": "查看日志尾部",
  "Diagnose": "诊断",
  "Context": "上下文",
  "View": "查看",
  "Stop": "停止",
  "Stop process": "停止进程",
  "Force process kill": "强制终止进程",
  "Cancel": "取消",
  "Stop process?": "停止进程？",
  "This stops the single PID shown below. Child processes may continue unless this is a LabGPU-tracked run.": "这只会停止下面这个 PID。除非这是 LabGPU 跟踪的任务，否则子进程可能继续运行。",
  "actions disabled": "操作已禁用",
  "shared account": "共享账号",
  "No LabGPU run or own GPU process found yet.": "还没有发现 LabGPU 任务或自己的 GPU 进程。",
  "No clearly free GPU found.": "没有找到明确空闲的 GPU。",
  "View busy GPUs": "查看忙碌 GPU",
  "View suspected idle GPUs": "查看疑似空转 GPU",
  "View all GPUs": "查看全部 GPU",
  "Agentless Own GPU Processes": "无代理检测到的我的 GPU 进程",
  "No GPU process owned by the SSH user.": "没有发现当前 SSH 用户拥有的 GPU 进程。",
  "All Alerts": "全部告警"
}};
const reverseTranslations = Object.fromEntries(Object.entries(translations).map(([en, zh]) => [zh, en]));
function currentLanguage() {{
  return localStorage.getItem("labgpu-language") || "en";
}}
function translateText(text, lang) {{
  const trimmed = text.trim();
  if (!trimmed) return text;
  const leading = text.match(/^\\s*/)[0];
  const trailing = text.match(/\\s*$/)[0];
  if (lang === "zh" && translations[trimmed]) return leading + translations[trimmed] + trailing;
  if (lang === "en" && reverseTranslations[trimmed]) return leading + reverseTranslations[trimmed] + trailing;
  return text;
}}
function applyLanguage(lang) {{
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  if (languageButton) languageButton.textContent = lang === "zh" ? "EN" : "中文";
  const skip = new Set(["SCRIPT", "STYLE", "CODE", "PRE", "TEXTAREA"]);
  document.querySelectorAll("body *").forEach((element) => {{
    if (skip.has(element.tagName) || element.id === "theme-toggle" || element.id === "language-toggle") return;
    element.childNodes.forEach((node) => {{
      if (node.nodeType === Node.TEXT_NODE) node.nodeValue = translateText(node.nodeValue || "", lang);
    }});
  }});
  updateThemeButton();
}}
function updateThemeButton() {{
  if (!themeButton) return;
  const dark = document.documentElement.dataset.theme === "dark";
  const zh = currentLanguage() === "zh";
  themeButton.textContent = zh ? (dark ? "浅色" : "深色") : (dark ? "Light" : "Dark");
}}
function applyTheme(theme) {{
  const dark = theme === "dark" || (theme === "system" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  updateThemeButton();
}}
function applyJsonPreference() {{
  const enabled = localStorage.getItem("labgpu-show-json") === "1";
  document.documentElement.classList.toggle("show-json", enabled);
  if (jsonToggle) jsonToggle.checked = enabled;
}}
function setRefreshPaused(value) {{
  paused = value;
  if (btn) btn.textContent = translateText(paused ? "Resume refresh" : "Pause refresh", currentLanguage());
}}
function stableElementKey(element, index) {{
  const form = element.closest("form");
  const anchor = element.id || (form && form.id) || element.dataset.persistKey || "page";
  const summary = element.querySelector("summary");
  const label = summary ? summary.textContent.trim() : "";
  return `labgpu-details:${{window.location.pathname}}:${{anchor}}:${{label || index}}`;
}}
function restoreDetailsState() {{
  document.querySelectorAll("details").forEach((details, index) => {{
    const key = stableElementKey(details, index);
    const saved = localStorage.getItem(key);
    if (saved !== null) details.open = saved === "1";
    details.addEventListener("toggle", () => {{
      localStorage.setItem(key, details.open ? "1" : "0");
    }});
  }});
}}
try {{
  applyJsonPreference();
  applyTheme(localStorage.getItem("labgpu-theme") || "system");
  applyLanguage(currentLanguage());
  restoreDetailsState();
  if (themeButton) {{
    themeButton.addEventListener("click", () => {{
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      localStorage.setItem("labgpu-theme", next);
      applyTheme(next);
    }});
  }}
  if (languageButton) {{
    languageButton.addEventListener("click", () => {{
      const next = currentLanguage() === "zh" ? "en" : "zh";
      localStorage.setItem("labgpu-language", next);
      applyLanguage(next);
    }});
  }}
  if (jsonToggle) {{
    jsonToggle.addEventListener("change", () => {{
      localStorage.setItem("labgpu-show-json", jsonToggle.checked ? "1" : "0");
      applyJsonPreference();
    }});
  }}
}} catch (error) {{}}
const btn = document.getElementById("pause-refresh");
if (btn) {{
  btn.addEventListener("click", () => {{
    setRefreshPaused(!paused);
  }});
}}
const refreshNow = document.getElementById("refresh-now");
if (refreshNow) {{
  refreshNow.addEventListener("click", () => {{
    const url = new URL(window.location.href);
    url.searchParams.set("refresh", "1");
    window.location.href = url.toString();
  }});
}}
setInterval(() => {{
  if (!paused) window.location.reload();
}}, 15000);
document.querySelectorAll("form input, form select, form textarea").forEach((input) => {{
  input.addEventListener("focus", () => setRefreshPaused(true), {{once: true}});
  input.addEventListener("input", () => setRefreshPaused(true), {{once: true}});
}});
document.querySelectorAll("[data-stop]").forEach((button) => {{
  button.addEventListener("click", async () => {{
    selectedStopButton = button;
    fillStopModal(button);
    const dialog = document.getElementById("stop-modal");
    if (dialog && dialog.showModal) dialog.showModal();
    else if (window.confirm("Stop process?")) runStop(false);
  }});
}});
async function copyTextFromButton(button) {{
  try {{
    await navigator.clipboard.writeText(button.dataset.copy || "");
    const original = button.textContent || "Copy";
    button.textContent = currentLanguage() === "zh" ? "已复制" : "Copied";
    setTimeout(() => button.textContent = original, 1200);
  }} catch (error) {{
    window.prompt("Copy command", button.dataset.copy || "");
  }}
}}
document.addEventListener("click", (event) => {{
  const target = event.target;
  const button = target && target.closest ? target.closest("[data-copy]") : null;
  if (!button) return;
  copyTextFromButton(button);
}});
function selectedAgent() {{
  const selected = document.querySelector('input[name="ssh-agent"]:checked');
  return selected ? selected.value : "claude";
}}
function selectedCcswitchProviderId() {{
  return "";
}}
function selectedAiMode() {{
  const selected = document.querySelector('input[name="ssh-ai-mode"]:checked');
  return selected ? selected.value : "proxy_tunnel";
}}
function currentCcswitchProviderName(agent) {{
  const providers = ccswitchSummary && ccswitchSummary.providers ? ccswitchSummary.providers : {{}};
  const provider = providers[agent] || {{}};
  return provider.current || "";
}}
function selectedGpuIndex() {{
  const gpu = document.getElementById("ssh-gpu");
  return gpu ? gpu.value : "";
}}
function selectedRemoteCwd() {{
  const input = document.getElementById("ssh-remote-cwd");
  return input ? input.value.trim() : "";
}}
async function loadVscodeRecentFolders(server) {{
  const input = document.getElementById("ssh-remote-cwd");
  const options = document.getElementById("ssh-remote-cwd-options");
  const summary = document.getElementById("ssh-folder-summary");
  if (options) options.innerHTML = "";
  if (summary) summary.textContent = "Loading VS Code Remote-SSH recent folders...";
  try {{
    if (!vscodeRecentFolders.length) {{
      const response = await fetch("/api/integrations/vscode/recent-folders");
      const payload = await response.json();
      vscodeRecentFolders = Array.isArray(payload.folders) ? payload.folders : [];
    }}
  }} catch (error) {{
    vscodeRecentFolders = [];
  }}
  const matches = vscodeRecentFolders.filter((item) => item && item.server_alias === server && item.path);
  if (options) {{
    matches.slice(0, 20).forEach((item) => {{
      const option = document.createElement("option");
      option.value = item.path;
      option.label = item.label || item.path;
      options.appendChild(option);
    }});
  }}
  if (input && !input.value && matches.length) input.value = matches[0].path;
  if (summary) {{
    summary.textContent = matches.length
      ? `Imported ${{matches.length}} VS Code recent folder${{matches.length === 1 ? "" : "s"}} for ${{server}}.`
      : "No VS Code recent folder found for this server. You can type an absolute remote path.";
  }}
}}
function ccswitchProxyPort(agent) {{
  const proxyConfig = activeCcswitchProxyConfig(agent);
  return proxyConfig && proxyConfig.listen_port ? String(proxyConfig.listen_port) : "";
}}
function activeCcswitchProxyConfig(agent) {{
  const proxy = ccswitchSummary && ccswitchSummary.proxy ? ccswitchSummary.proxy : {{}};
  const proxyConfig = agent && agent !== "none" ? proxy[agent] : proxy.codex || proxy.claude || proxy.gemini || proxy.openclaw;
  if (!proxyConfig || !proxyConfig.listen_port) return null;
  return proxyConfig.enabled || proxyConfig.proxy_enabled ? proxyConfig : null;
}}
function ccswitchProxyIsListening(proxyConfig) {{
  if (!proxyConfig) return false;
  return proxyConfig.listening !== false;
}}
function ccswitchProxyStatus(agent) {{
  const proxy = ccswitchSummary && ccswitchSummary.proxy ? ccswitchSummary.proxy : {{}};
  const proxyConfig = agent && agent !== "none" ? proxy[agent] : proxy.codex || proxy.claude || proxy.gemini || proxy.openclaw;
  if (!proxyConfig || !proxyConfig.listen_port) return "proxy: -";
  let state = proxyConfig.enabled || proxyConfig.proxy_enabled ? "enabled" : "disabled";
  if ((proxyConfig.enabled || proxyConfig.proxy_enabled) && !ccswitchProxyIsListening(proxyConfig)) state = "enabled, not listening";
  else if ((proxyConfig.enabled || proxyConfig.proxy_enabled) && proxyConfig.listening == null) state = "enabled, TCP check unknown";
  return `proxy ${{proxyConfig.listen_address || "127.0.0.1"}}:${{proxyConfig.listen_port}} (${{state}})`;
}}
function describeCcswitch(summary) {{
  if (!summary || !summary.available) return summary && summary.message ? summary.message : "CC Switch not detected.";
  const providers = summary.providers || {{}};
  const proxy = summary.proxy || {{}};
  const labels = {{codex: "Codex", claude: "Claude", gemini: "Gemini", openclaw: "OpenClaw"}};
  const providerText = ["codex", "claude", "gemini", "openclaw"].map((name) => {{
    const current = providers[name] && providers[name].current ? providers[name].current : "-";
    return `${{labels[name]}}: ${{current}}`;
  }}).join(" · ");
  const proxyText = ccswitchProxyStatus(selectedAgent());
  return `${{providerText}} · ${{proxyText}}`;
}}
function updateCcswitchProviderOptions() {{
  const agent = selectedAgent();
  const providerSummary = document.getElementById("ssh-provider-summary");
  const proxySummary = document.getElementById("ssh-proxy-summary");
  const providerName = currentCcswitchProviderName(agent);
  const proxyConfig = activeCcswitchProxyConfig(agent);
  if (providerSummary) {{
    providerSummary.textContent = providerName
      ? `Using current CC Switch Claude provider: ${{providerName}}. To change provider, switch it in CC Switch first.`
      : "Current CC Switch Claude provider was not found. Switch Claude provider in CC Switch first.";
  }}
  if (proxySummary) {{
    proxySummary.textContent = proxyConfig && proxyConfig.listen_port && ccswitchProxyIsListening(proxyConfig)
      ? `Proxy Tunnel: remote random port -> local LabGPU gateway -> CC Switch 127.0.0.1:${{proxyConfig.listen_port}}`
      : proxyConfig && proxyConfig.listen_port
        ? `Proxy Tunnel: CC Switch proxy is configured but not listening on 127.0.0.1:${{proxyConfig.listen_port}}.`
      : "Proxy Tunnel: CC Switch proxy is not configured or not enabled.";
  }}
}}
async function loadCcswitchSummary() {{
  const status = document.getElementById("ssh-ccswitch-status");
  try {{
    const response = await fetch("/api/integrations/ccswitch");
    ccswitchSummary = await response.json();
  }} catch (error) {{
    ccswitchSummary = {{available: false, message: "CC Switch not detected."}};
  }}
  updateCcswitchProviderOptions();
  if (status) status.textContent = describeCcswitch(ccswitchSummary);
  return ccswitchSummary;
}}
function selectedLocalProxyPort() {{
  if (selectedAiMode() !== "proxy_tunnel") return "";
  return ccswitchProxyPort(selectedAgent());
}}
function selectedRemoteProxyPort() {{
  return "";
}}
function updateSshProxyFields() {{
  updateCcswitchProviderOptions();
}}
async function runOpenSsh(button) {{
  const original = button.textContent || "Enter Server";
  const result = document.getElementById("ssh-modal-result");
  const agent = selectedAgent();
  const mode = selectedAiMode();
  const providerName = currentCcswitchProviderName(agent);
  if (agent !== "claude") {{
    if (result) result.textContent = "Only Claude Code AI sessions are available in this alpha.";
    return;
  }}
  if (mode !== "proxy_tunnel") {{
    if (result) result.textContent = "Remote Write is not available in this alpha.";
    return;
  }}
  if (!providerName) {{
    if (result) result.textContent = "Current CC Switch Claude provider was not found. Switch Claude provider in CC Switch first.";
    return;
  }}
  if (!activeCcswitchProxyConfig(agent)) {{
    if (result) result.textContent = "CC Switch proxy is not running or not configured for Claude. Start CC Switch proxy first, then reopen this session.";
    return;
  }}
  const proxyConfig = activeCcswitchProxyConfig(agent);
  if (!ccswitchProxyIsListening(proxyConfig)) {{
    if (result) result.textContent = `CC Switch proxy is configured but not listening on 127.0.0.1:${{proxyConfig.listen_port}}.`;
    return;
  }}
  button.textContent = translateText("Opening terminal...", currentLanguage());
  button.disabled = true;
  const response = await fetch(`/api/servers/${{encodeURIComponent(button.dataset.openSsh || "")}}/open-ssh`, {{
    method: "POST",
    headers: {{"X-LabGPU-Action-Token": actionToken, "Content-Type": "application/json"}},
    body: JSON.stringify({{
      local_proxy_port: selectedLocalProxyPort(),
      remote_proxy_port: selectedRemoteProxyPort(),
      agent: agent,
      ai_mode: mode,
      provider_name: providerName,
      gpu_index: selectedGpuIndex(),
      remote_cwd: selectedRemoteCwd(),
      ccswitch_provider_id: selectedCcswitchProviderId()
    }})
  }});
  const payload = await response.json().catch(() => ({{ok: false, message: "Opening terminal failed."}}));
  button.disabled = false;
  if (payload.ok) {{
    button.textContent = translateText("Terminal opened", currentLanguage());
    rememberAiSession(button, providerName, payload.ai_gateway || {{}});
    setTimeout(() => button.textContent = original, 1400);
    const dialog = document.getElementById("ssh-modal");
    if (dialog && dialog.open) dialog.close();
  }} else {{
    button.textContent = original;
    if (result) result.textContent = payload.message || "Opening terminal failed.";
    else window.alert(payload.message || "Opening terminal failed.");
  }}
}}
function rememberAiSession(button, providerName, gateway) {{
  const localPort = selectedLocalProxyPort();
  const entry = {{
    server: button.dataset.openSsh || "",
    app: "Claude Code",
    provider: providerName || "-",
    ccswitchProxyPort: gateway.ccswitch_proxy_port || localPort,
    localGatewayPort: gateway.local_gateway_port || "-",
    remoteGatewayPort: gateway.remote_gateway_port || "-",
    tokenFingerprint: gateway.token_fingerprint || "",
    gpu: selectedGpuIndex() || "none",
    cwd: selectedRemoteCwd() || gateway.remote_cwd || "",
    startedAt: new Date().toLocaleString()
  }};
  let rows = [];
  try {{ rows = JSON.parse(localStorage.getItem("labgpu-ai-sessions") || "[]"); }} catch (error) {{ rows = []; }}
  rows.unshift(entry);
  localStorage.setItem("labgpu-ai-sessions", JSON.stringify(rows.slice(0, 8)));
  renderAiSessions();
}}
function renderAiSessions() {{
  const target = document.getElementById("ai-session-rows");
  if (!target) return;
  let rows = [];
  try {{ rows = JSON.parse(localStorage.getItem("labgpu-ai-sessions") || "[]"); }} catch (error) {{ rows = []; }}
  if (!rows.length) {{
    target.innerHTML = "<tr><td colspan='6' class='muted'>No AI sessions launched from this browser yet.</td></tr>";
    return;
  }}
  target.innerHTML = rows.map((item) => `
    <tr>
      <td>${{escapeHtml(item.server || "-")}}</td>
      <td><code>${{escapeHtml(item.cwd || "-")}}</code></td>
      <td>${{escapeHtml(item.app || "-")}} / ${{escapeHtml(item.provider || "-")}}</td>
      <td>remote 127.0.0.1:${{escapeHtml(item.remoteGatewayPort || "-")}} -> local gateway 127.0.0.1:${{escapeHtml(item.localGatewayPort || "-")}} -> CC Switch 127.0.0.1:${{escapeHtml(item.ccswitchProxyPort || "-")}}</td>
      <td>${{escapeHtml(item.gpu || "none")}}</td>
      <td>${{escapeHtml(item.startedAt || "-")}}</td>
    </tr>
  `).join("");
}}
function escapeHtml(value) {{
  return String(value).replace(/[&<>"']/g, (ch) => ({{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}}[ch]));
}}
document.addEventListener("click", async (event) => {{
  const target = event.target;
  const button = target && target.closest ? target.closest("[data-open-ssh]") : null;
  if (!button) return;
  selectedSshButton = button;
  const dialog = document.getElementById("ssh-modal");
  if (!dialog || !dialog.showModal) {{
    await runOpenSsh(button);
    return;
  }}
  const serverCell = document.getElementById("ssh-modal-server");
  const gpuSelect = document.getElementById("ssh-gpu");
  const cwdInput = document.getElementById("ssh-remote-cwd");
  const result = document.getElementById("ssh-modal-result");
  if (serverCell) serverCell.textContent = button.dataset.openSsh || "";
  if (cwdInput) cwdInput.value = "";
  if (gpuSelect) {{
    const selectedGpu = button.dataset.gpuIndex || "";
    gpuSelect.innerHTML = "<option value=''>none</option>";
    if (selectedGpu) {{
      const option = document.createElement("option");
      option.value = selectedGpu;
      option.textContent = `GPU ${{selectedGpu}}`;
      option.selected = true;
      gpuSelect.appendChild(option);
    }}
  }}
  if (result) result.textContent = "";
  updateSshProxyFields();
  loadCcswitchSummary();
  loadVscodeRecentFolders(button.dataset.openSsh || "");
  setRefreshPaused(true);
  dialog.showModal();
}});
renderAiSessions();
document.querySelectorAll("[data-alert-action]").forEach((button) => {{
  button.addEventListener("click", async () => {{
    const key = button.dataset.alertKey;
    const action = button.dataset.alertAction;
    const response = await fetch(`/api/alerts/${{encodeURIComponent(key)}}/${{action}}`, {{
      method: "POST",
      headers: {{"X-LabGPU-Action-Token": actionToken}}
    }});
    if (response.ok) window.location.reload();
    else window.alert("Alert action failed.");
  }});
}});
const settingsImport = document.getElementById("settings-import");
if (settingsImport) {{
  settingsImport.addEventListener("submit", async (event) => {{
    event.preventDefault();
    const form = new FormData(settingsImport);
    const response = await fetch("/api/settings/import-ssh", {{
      method: "POST",
      body: new URLSearchParams(form)
    }});
    if (response.ok) {{
      window.alert("Saved server inventory.");
      window.location.reload();
    }} else {{
      window.alert("Saving settings failed.");
    }}
  }});
}}
const settingsAddServer = document.getElementById("settings-add-server");
if (settingsAddServer) {{
  settingsAddServer.addEventListener("submit", async (event) => {{
    event.preventDefault();
    const form = new FormData(settingsAddServer);
    const response = await fetch("/api/settings/add-server", {{
      method: "POST",
      body: new URLSearchParams(form)
    }});
    const payload = await response.json().catch(() => ({{}}));
    if (response.ok) {{
      const backup = payload.backup ? `\\nBackup: ${{payload.backup}}` : "";
      window.alert(`Saved ${{payload.alias || "server"}}.\\nSSH config: ${{payload.ssh_config || "-"}}${{backup}}`);
      window.location.reload();
    }} else {{
      window.alert(payload.message || payload.error || "Adding server failed.");
    }}
  }});
}}
document.querySelectorAll(".settings-groups-form").forEach((settingsGroups) => {{
  settingsGroups.addEventListener("submit", async (event) => {{
    event.preventDefault();
    const form = new FormData(settingsGroups);
    const response = await fetch("/api/settings/groups", {{
      method: "POST",
      body: new URLSearchParams(form)
    }});
    const payload = await response.json().catch(() => ({{}}));
    if (response.ok) {{
      const groupText = payload.group ? ` to "${{payload.group}}"` : "";
      const count = (payload.updated || []).length;
      window.alert(count ? `Saved ${{count}} server(s)${{groupText}}.` : `Saved group "${{payload.group || "ungrouped"}}".`);
      window.location.reload();
    }} else {{
      window.alert(payload.message || payload.error || "Saving groups failed.");
    }}
  }});
}});
const settingsDeleteGroups = document.getElementById("settings-delete-groups");
if (settingsDeleteGroups) {{
  settingsDeleteGroups.addEventListener("submit", async (event) => {{
    event.preventDefault();
    const form = new FormData(settingsDeleteGroups);
    const names = form.getAll("groups");
    if (!names.length) {{
      window.alert("Select at least one group name.");
      return;
    }}
    if (!window.confirm(`Delete selected group name(s)? Servers will stay saved and move to ungrouped.`)) return;
    const response = await fetch("/api/settings/groups/delete", {{
      method: "POST",
      body: new URLSearchParams(form)
    }});
    const payload = await response.json().catch(() => ({{}}));
    if (response.ok) {{
      window.alert(`Deleted ${{(payload.deleted || []).length}} group name(s).`);
      window.location.reload();
    }} else {{
      window.alert(payload.message || payload.error || "Deleting groups failed.");
    }}
  }});
}}
const assistantForm = document.getElementById("assistant-form");
const assistantInput = document.getElementById("assistant-input");
const assistantChat = document.getElementById("assistant-chat");
const assistantUseApi = document.getElementById("assistant-use-api");
const assistantApiUrl = document.getElementById("assistant-api-url");
const assistantModel = document.getElementById("assistant-model");
const assistantApiKey = document.getElementById("assistant-api-key");
const assistantRememberKey = document.getElementById("assistant-remember-key");
function loadAssistantSettings() {{
  if (assistantUseApi) assistantUseApi.checked = localStorage.getItem("labgpu-assistant-use-api") === "1";
  if (assistantApiUrl) assistantApiUrl.value = localStorage.getItem("labgpu-assistant-api-url") || "";
  if (assistantModel) assistantModel.value = localStorage.getItem("labgpu-assistant-model") || "";
  if (assistantRememberKey) assistantRememberKey.checked = localStorage.getItem("labgpu-assistant-remember-key") === "1";
  if (assistantApiKey && assistantRememberKey && assistantRememberKey.checked) {{
    assistantApiKey.value = localStorage.getItem("labgpu-assistant-api-key") || "";
  }}
}}
function saveAssistantSettings() {{
  if (assistantUseApi) localStorage.setItem("labgpu-assistant-use-api", assistantUseApi.checked ? "1" : "0");
  if (assistantApiUrl) localStorage.setItem("labgpu-assistant-api-url", assistantApiUrl.value.trim());
  if (assistantModel) localStorage.setItem("labgpu-assistant-model", assistantModel.value.trim());
  if (assistantRememberKey) localStorage.setItem("labgpu-assistant-remember-key", assistantRememberKey.checked ? "1" : "0");
  if (assistantApiKey && assistantRememberKey && assistantRememberKey.checked) {{
    localStorage.setItem("labgpu-assistant-api-key", assistantApiKey.value);
  }} else {{
    localStorage.removeItem("labgpu-assistant-api-key");
  }}
}}
function readAssistantSettings() {{
  saveAssistantSettings();
  return {{
    mode: assistantUseApi && assistantUseApi.checked ? "api" : "local",
    api_url: assistantApiUrl ? assistantApiUrl.value.trim() : "",
    model: assistantModel ? assistantModel.value.trim() : "",
    api_key: assistantApiKey ? assistantApiKey.value : ""
  }};
}}
loadAssistantSettings();
[assistantUseApi, assistantApiUrl, assistantModel, assistantApiKey, assistantRememberKey].forEach((node) => {{
  if (!node) return;
  const eventName = node.tagName === "INPUT" && node.type === "text" ? "input" : "change";
  node.addEventListener(eventName, saveAssistantSettings);
}});
function appendAssistantMessage(kind, text, copy) {{
  if (!assistantChat) return;
  const node = document.createElement("div");
  node.className = `assistant-message assistant-message-${{kind}}`;
  const textNode = document.createElement("div");
  textNode.textContent = text || "";
  node.appendChild(textNode);
  if (copy) {{
    const actions = document.createElement("div");
    actions.className = "actions";
    actions.style.marginTop = "8px";
    const button = document.createElement("button");
    button.className = "small";
    button.type = "button";
    button.dataset.copy = copy;
    button.textContent = currentLanguage() === "zh" ? "复制" : "Copy";
    actions.appendChild(button);
    node.appendChild(actions);
  }}
  assistantChat.appendChild(node);
  assistantChat.scrollTop = assistantChat.scrollHeight;
}}
async function askAssistant(message) {{
  appendAssistantMessage("user", message);
  appendAssistantMessage("assistant", "Thinking...");
  const pending = assistantChat ? assistantChat.lastElementChild : null;
  const response = await fetch("/api/assistant/chat", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{message, assistant: readAssistantSettings()}})
  }});
  const payload = await response.json().catch(() => ({{ok: false, reply: "Assistant request failed."}}));
  if (pending) pending.remove();
  appendAssistantMessage("assistant", payload.reply || "No answer.", payload.copy || "");
}}
if (assistantForm && assistantInput) {{
  assistantForm.addEventListener("submit", async (event) => {{
    event.preventDefault();
    const message = assistantInput.value.trim();
    if (!message) return;
    assistantInput.value = "";
    await askAssistant(message);
  }});
}}
document.querySelectorAll("[data-assistant-example]").forEach((button) => {{
  button.addEventListener("click", () => {{
    if (!assistantInput) return;
    assistantInput.value = button.dataset.assistantExample || "";
    assistantInput.focus();
  }});
}});
const watchEnable = document.getElementById("watch-enable");
const watchClear = document.getElementById("watch-clear");
const watchStatus = document.getElementById("watch-status");
function readWatch() {{
  try {{ return JSON.parse(localStorage.getItem("labgpu-gpu-watch") || "null"); }} catch (error) {{ return null; }}
}}
function setWatchStatus() {{
  const watch = readWatch();
  if (watchStatus) watchStatus.textContent = watch ? `Watching model=${{watch.model || "*"}}, min=${{watch.minMemGb || "0"}}GB, tag=${{watch.tag || "*"}}` : translateText("No browser watch configured.", currentLanguage());
}}
if (watchEnable) {{
  watchEnable.addEventListener("click", async () => {{
    if ("Notification" in window && Notification.permission === "default") await Notification.requestPermission();
    const watch = {{
      model: document.getElementById("watch-model").value.trim(),
      minMemGb: document.getElementById("watch-min-mem").value.trim(),
      tag: document.getElementById("watch-tag").value.trim(),
      notified: false
    }};
    localStorage.setItem("labgpu-gpu-watch", JSON.stringify(watch));
    setWatchStatus();
    checkGpuWatch();
  }});
}}
if (watchClear) {{
  watchClear.addEventListener("click", () => {{
    localStorage.removeItem("labgpu-gpu-watch");
    setWatchStatus();
  }});
}}
function checkGpuWatch() {{
  const watch = readWatch();
  if (!watch) return;
  const minMb = Number.parseFloat(watch.minMemGb || "0") * 1024;
  const model = (watch.model || "").toLowerCase();
  const tag = (watch.tag || "").toLowerCase();
  const hit = Array.from(document.querySelectorAll("[data-gpu-choice]")).find((node) => {{
    const free = Number.parseInt(node.dataset.freeMb || "0", 10);
    const name = (node.dataset.model || "").toLowerCase();
    const tags = (node.dataset.tags || "").toLowerCase();
    return free >= minMb && (!model || name.includes(model)) && (!tag || tags.includes(tag));
  }});
  if (hit && !watch.notified) {{
    const freeGb = Number.parseInt(hit.dataset.freeMb || "0", 10) / 1024;
    const freeText = freeGb ? ` · ${{freeGb.toFixed(1)}}GB free` : "";
    const modelText = hit.dataset.model ? ` · ${{hit.dataset.model}}` : "";
    const message = `${{hit.dataset.server}} GPU ${{hit.dataset.gpuIndex}} is available${{modelText}}${{freeText}}`;
    if ("Notification" in window && Notification.permission === "granted") new Notification("LabGPU", {{body: message}});
    else window.alert(message);
    watch.notified = true;
    localStorage.setItem("labgpu-gpu-watch", JSON.stringify(watch));
  }}
}}
setWatchStatus();
checkGpuWatch();
const modalCancel = document.getElementById("modal-cancel");
if (modalCancel) modalCancel.addEventListener("click", () => document.getElementById("stop-modal").close());
const modalStop = document.getElementById("modal-stop");
if (modalStop) modalStop.addEventListener("click", () => runStop(false));
const modalForce = document.getElementById("modal-force");
if (modalForce) modalForce.addEventListener("click", () => runStop(true));
const sshModalCancel = document.getElementById("ssh-modal-cancel");
if (sshModalCancel) sshModalCancel.addEventListener("click", () => document.getElementById("ssh-modal").close());
const sshModalOpen = document.getElementById("ssh-modal-open");
if (sshModalOpen) sshModalOpen.addEventListener("click", async () => {{
  if (selectedSshButton) await runOpenSsh(selectedSshButton);
}});
const sshProxySelect = document.getElementById("ssh-proxy");
if (sshProxySelect) sshProxySelect.addEventListener("change", updateSshProxyFields);
const sshOptionInputs = document.querySelectorAll('input[name="ssh-agent"], input[name="ssh-ai-mode"]');
sshOptionInputs.forEach((input) => input.addEventListener("change", loadCcswitchSummary));
if (sshProxySelect || sshOptionInputs.length) loadCcswitchSummary();
function fillStopModal(button) {{
  const pairs = {{
    "modal-server": button.dataset.server || "-",
    "modal-gpu": button.dataset.gpu || "-",
    "modal-pid": button.dataset.pid || "-",
    "modal-user": button.dataset.user || "-",
    "modal-runtime": button.dataset.runtime || "-",
    "modal-memory": button.dataset.memory || "-",
    "modal-command": button.dataset.command || "-"
  }};
  for (const [id, value] of Object.entries(pairs)) {{
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }}
  const result = document.getElementById("modal-result");
  if (result) result.textContent = "";
  const force = document.getElementById("modal-force");
  if (force) force.hidden = true;
}}
async function runStop(force) {{
  if (!selectedStopButton) return;
  const result = document.getElementById("modal-result");
  if (result) result.textContent = force ? "Sending SIGKILL..." : "Sending SIGTERM...";
  const payload = await stopProcess(selectedStopButton, force);
  if (result) result.textContent = payload.message || payload.result || "done";
  const forceButton = document.getElementById("modal-force");
  if (!payload.ok && payload.result === "alive" && forceButton) forceButton.hidden = false;
  if (payload.ok) setTimeout(() => window.location.reload(), 800);
}}
async function stopProcess(button, force) {{
    const path = `/api/servers/${{encodeURIComponent(button.dataset.server)}}/processes/${{button.dataset.pid}}/${{force ? "force-stop" : "stop"}}`;
    const response = await fetch(path, {{
      method: "POST",
      headers: {{"Content-Type": "application/json", "X-LabGPU-Action-Token": actionToken}},
      body: JSON.stringify({{
        expected_user: button.dataset.user,
        expected_start_time: button.dataset.start,
        expected_command_hash: button.dataset.hash
      }})
    }});
    return await response.json().catch(() => ({{ok: false, message: "request failed"}}));
}}
</script>
</body></html>"""


def split_hosts(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def known_ssh_aliases(ssh_config: str | Path | None = None) -> set[str]:
    aliases = {host.alias for host in parse_ssh_config(ssh_config)}
    for entry in load_config().servers.values():
        aliases.add(entry.alias)
        aliases.add(entry.name)
    return {alias for alias in aliases if alias}


def render_nav(*, status: str = "", json_href: str = "/api/servers") -> str:
    return f"""
    <nav class="topbar">
      <div class="topnav" aria-label="Primary navigation">
        <a href="/">Overview</a>
        <a href="/gpus">Train Now</a>
        <a href="/me">My Training</a>
        <a href="/assistant">Assistant</a>
        <a href="/providers">AI Providers</a>
        <a href="/servers">Servers</a>
        <a href="/groups">Groups</a>
        <a href="/alerts">Alerts</a>
        <a href="/settings">Settings</a>
      </div>
      <div class="top-controls" aria-label="Display and refresh controls">
        {status}
        <button id="refresh-now" type="button">Refresh now</button>
        <button id="pause-refresh" type="button">Pause refresh</button>
        <a class="json-control" href="{esc(json_href)}">JSON</a>
        <button id="language-toggle" type="button">中文</button>
        <button id="theme-toggle" type="button">Dark</button>
      </div>
    </nav>
    """


def section_head(title: str, view_all: str | None = None) -> str:
    link = f"<a href='{esc(view_all)}'>View all</a>" if view_all else ""
    return f"<div class='section-head'><h2>{esc(title)}</h2>{link}</div>"


def page_url(path: str, ui: dict[str, object] | None = None, **params: object) -> str:
    query: dict[str, str] = {}
    group = str((ui or {}).get("group") or "").strip()
    if group and group != "all":
        query["group"] = group
    for key, value in params.items():
        text = str(value or "").strip()
        if text:
            query[key] = text
    if not query:
        return path
    return f"{path}?{urlencode(query)}"


def preview_list(values: object, limit: int) -> list[object]:
    if not isinstance(values, list):
        return []
    return values[:limit]


def option(value: str, label: str, selected: str) -> str:
    return f"<option value='{esc(value)}' {'selected' if value == selected else ''}>{esc(label)}</option>"


def checked(value: object) -> str:
    return "checked" if str(value or "") in {"1", "true", "yes", "on"} else ""


def load_sort_key(value: object) -> tuple[float, str]:
    return ranking.load_sort_key(value)


def alert_rank(value: object) -> int:
    return {"error": 0, "warning": 1, "info": 2}.get(str(value), 3)


def gpu_recommendation(item: dict[str, object]) -> dict[str, str]:
    return ranking.gpu_recommendation(item)


def recommendation_score(item: dict[str, object]) -> int:
    return ranking.recommendation_score(item)


def gpu_recommendation_sort_key(item: dict[str, object]) -> tuple[int, float, int]:
    key = ranking.gpu_recommendation_sort_key(item)
    return (key[0], key[2], key[3])


def load_ratio_value(value: object) -> float:
    return ranking.load_ratio_value(value)


def load_value(value: object) -> str:
    return ranking.load_value(value)


def format_memory(value: object) -> str:
    return ranking.format_memory(value)


def process_state_label(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    state = text[0]
    return {
        "R": "running",
        "S": "sleeping",
        "D": "io wait",
        "Z": "zombie",
        "T": "stopped",
        "I": "idle",
    }.get(state, "unknown")


def user_label(value: object) -> str:
    text = str(value or "").strip()
    if not text or text == "?":
        return "unknown"
    return text


def probe_seconds(value: object) -> float:
    try:
        return float(value) / 1000
    except (TypeError, ValueError):
        return 0.0


def format_latency(value: object) -> str:
    seconds = probe_seconds(value)
    if seconds >= 1:
        return f"{seconds:.1f}s"
    return f"{int(seconds * 1000)}ms"


def server_health(host: dict[str, object]) -> str:
    alerts = host.get("alerts") if isinstance(host.get("alerts"), list) else []
    if any(isinstance(alert, dict) and alert.get("severity") == "error" for alert in alerts):
        return "critical"
    if any(isinstance(alert, dict) and alert.get("severity") == "warning" for alert in alerts):
        return "warning"
    return "ok"


def relative_time(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    normalized = text.replace("Z", "+00:00")
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def first_value(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def truthy(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).lower() in {"1", "true", "yes", "on"}


def short(value: object, width: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def gpu_summary(gpus: object) -> dict[str, int]:
    if not isinstance(gpus, list):
        return {"total": 0, "free": 0, "busy": 0}
    total = len(gpus)
    busy = sum(1 for gpu in gpus if isinstance(gpu, dict) and gpu.get("processes"))
    return {"total": total, "free": max(0, total - busy), "busy": busy}


def load_label(host: dict[str, object]) -> str:
    load_avg = host.get("load_avg") if isinstance(host.get("load_avg"), dict) else {}
    one = load_avg.get("1m") if isinstance(load_avg, dict) else None
    cores = host.get("cpu_cores")
    if one is None:
        return "-"
    if cores:
        return f"{one}/{cores}"
    return str(one)


def top_users(processes: object) -> str:
    if not isinstance(processes, list):
        return "-"
    counts: dict[str, int] = {}
    for proc in processes:
        if not isinstance(proc, dict):
            continue
        user = user_label(proc.get("user"))
        counts[user] = counts.get(user, 0) + 1
    if not counts:
        return "-"
    return ", ".join(name for name, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3])


def join_values(values: object) -> str:
    return ranking.join_values(values)


def is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}
