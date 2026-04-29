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
from urllib.parse import parse_qs, unquote, urlparse

from labgpu.core.config import ServerEntry, load_config, write_config
from labgpu.remote.actions import open_ssh_terminal, stop_process
from labgpu.remote.alerts import all_alert_records, apply_alert_state, set_alert_status
from labgpu.remote.assistant import assistant_reply
from labgpu.remote.cache import read_server_cache, write_server_cache
from labgpu.remote.demo import fake_lab_data
from labgpu.remote.history import append_history, apply_history_evidence, read_history
from labgpu.remote.inventory import load_inventory
from labgpu.remote.probe import probe_host
from labgpu.remote import ranking
from labgpu.remote.ssh_config import SSHHost, parse_ssh_config, resolve_ssh_host
from labgpu.remote.state import alerts_for_server, annotate_server, build_overview, human_duration
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
        data["hosts"] = hosts
        data["count"] = len(hosts)
        data["overview"] = build_overview(hosts)
        data["overview"]["all_alert_items"] = data["overview"].get("alert_items", [])
        data["inventory_mode"] = "demo"
        return data
    saved_config = load_config()
    using_saved_inventory = not names and not pattern and any(entry.enabled for entry in saved_config.servers.values())
    hosts = load_inventory(ssh_config=ssh_config, names=names, pattern=pattern)
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
        elif parsed.path == "/settings":
            self._html(render_settings_page(ssh_config=self.ssh_config))
        elif parsed.path == "/assistant":
            self._html(render_assistant_page(self._data(parsed.query)))
        elif parsed.path == "/api/overview":
            self._json(self._data(parsed.query))
        elif parsed.path == "/api/servers":
            self._json(self._data(parsed.query))
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
        refresh = truthy(params.get("refresh", ["0"])[0])
        data = collect_servers(
            ssh_config=self.ssh_config,
            names=names,
            pattern=pattern,
            timeout=self.timeout,
            fake_lab=self.fake_lab,
            use_cache=not refresh,
            background_refresh=not refresh,
        )
        data["scope_mode"] = scope_mode
        data["scope_hosts"] = names or []
        data["scope_pattern"] = pattern or ""
        data["ui"] = {
            "q": params.get("q", [""])[0].strip(),
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
        host = resolve_ssh_host(load_inventory(ssh_config=self.ssh_config, names=[alias])[0])
        result = open_ssh_terminal(host)
        self._json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT)

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
        disk_paths = split_csv(str(first_value(payload.get("disk_paths")) or "")) or ["/", "/home", "/data", "/scratch", "/mnt", "/nvme"]
        shared_account = truthy(first_value(payload.get("shared_account")))
        allow_stop = truthy(first_value(payload.get("allow_stop_own_process")), default=True)

        config = load_config()
        for entry in config.servers.values():
            entry.enabled = False
        for alias in aliases:
            entry = next((item for item in config.servers.values() if item.alias == alias), None) or ServerEntry(name=alias, alias=alias)
            entry.enabled = True
            entry.tags = tags
            entry.disk_paths = disk_paths
            entry.shared_account = shared_account
            entry.allow_stop_own_process = allow_stop
            config.servers[entry.name] = entry
        write_config(config)
        self._json({"ok": True, "imported": aliases})

    def _assistant_chat(self) -> None:
        payload = self._read_body_payload()
        message = str(payload.get("message") or "")
        data = self._data("")
        self._json(assistant_reply(data, message))

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
          <div class="actions">
            <button class="button" id="pause-refresh" type="button">Pause refresh</button>
            <a class="button" href="/api/servers">JSON</a>
          </div>
        </section>
        {render_data_status(data)}
        {render_overview(overview)}
        {render_train_now(overview, limit=4)}
        {render_my_training(training_items(hosts, overview), limit=8, view_all='/me')}
        {render_failure_inbox(failure_inbox_items(hosts, overview), limit=8)}
        {render_alerts(overview.get('alert_items') if isinstance(overview, dict) else [], limit=8, view_all='/alerts', title='Problems')}
        <section class="panel"><div class="section-head"><h2>Servers</h2><a href="/settings">Choose home servers</a></div><p class="muted">{esc(server_note)}</p><div class="grid compact">{cards}</div></section>
        """,
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
          <div class="actions"><button class="button" id="pause-refresh" type="button">Pause refresh</button><a class="button" href="/">Overview</a></div>
        </section>
        {render_data_status(data)}
        {render_filters(ui, kind='gpus')}
        {render_gpu_watch_panel(ui)}
        {render_gpu_finder(overview, ui)}
        """,
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
          <div class="actions"><button class="button" id="pause-refresh" type="button">Pause refresh</button><a class="button" href="/">Overview</a></div>
        </section>
        {render_data_status(data)}
        {render_process_filters(ui)}
        {render_my_training(training_items(hosts, overview), ui=ui)}
        {render_my_processes(overview.get('my_process_items') if isinstance(overview, dict) else [], ui=ui, title='Agentless Own GPU Processes')}
        """,
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
          <div class="actions"><button class="button" id="pause-refresh" type="button">Pause refresh</button><a class="button" href="/settings">Settings</a></div>
        </section>
        {render_data_status(data)}
        {render_server_filters(ui)}
        <section class="grid">{cards}</section>
        """,
    )


def render_alerts_page(data: dict[str, object]) -> str:
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    return page(
        "Alerts",
        f"""
        <section class="toolbar">
          <div>
            <h1>Alerts</h1>
            <p>Disk, SSH, GPU, and process conditions that need attention.</p>
          </div>
          <div class="actions"><button class="button" id="pause-refresh" type="button">Pause refresh</button><a class="button" href="/">Overview</a></div>
        </section>
        {render_data_status(data)}
        {render_alert_filters(ui)}
        {render_alerts(overview.get('all_alert_items') if isinstance(overview, dict) else [], ui=ui, title='All Alerts')}
        """,
    )


def render_settings_page(*, ssh_config: str | Path | None = None) -> str:
    config = load_config()
    ssh_hosts = parse_ssh_config(ssh_config)
    saved_enabled = {entry.alias for entry in config.servers.values() if entry.enabled}
    host_rows = "".join(
        f"<tr><td><label><input type='checkbox' name='aliases' value='{esc(host.alias)}' {'checked' if host.alias in saved_enabled else ''}> <code>{esc(host.alias)}</code></label></td><td>{esc(host.hostname or '-')}</td><td>{esc(host.user or '-')}</td><td>{esc(host.port or 22)}</td><td><a href='/servers/{esc(host.alias)}'>Test connection</a></td></tr>"
        for host in ssh_hosts
    ) or "<tr><td colspan='5' class='muted'>No SSH hosts found.</td></tr>"
    server_rows = "".join(
        f"<tr><td><code>{esc(entry.alias)}</code></td><td>{esc(entry.enabled)}</td><td>{esc(join_values(entry.tags))}</td><td>{esc(join_values(entry.disk_paths))}</td><td>{esc(entry.shared_account)}</td><td>{esc(entry.allow_stop_own_process)}</td></tr>"
        for entry in config.servers.values()
    ) or "<tr><td colspan='6' class='muted'>No saved LabGPU server inventory yet.</td></tr>"
    return page(
        "Settings",
        f"""
        <section class="toolbar">
          <div>
            <h1>Settings</h1>
            <p>Import SSH hosts and manage the server inventory stored in <code>~/.labgpu/config.toml</code>.</p>
          </div>
          <div class="actions"><a class="button" href="/">Overview</a><a class="button" href="/servers">Servers</a></div>
        </section>
        <section class="panel">
          <h2>Saved Servers</h2>
          <table><tr><th>Alias</th><th>Enabled</th><th>Tags</th><th>Disk paths</th><th>Shared account</th><th>Stop own process</th></tr>{server_rows}</table>
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
        """,
    )


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
          <div class="actions"><a class="button" href="/">Overview</a><a class="button" href="/gpus">Train Now</a></div>
        </section>
        {render_data_status(data)}
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
        {render_train_now(overview, limit=3)}
        """,
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
    if missing and refreshing:
        message = f"{missing} server has no cached snapshot yet. Background refresh is running."
    elif refreshing:
        message = f"Opening from local cache. Background refresh is running for {len(refreshing)} server(s). Oldest snapshot: {age}."
    else:
        message = f"Opening from local cache. Oldest snapshot: {age}."
    scope = scope_note(data)
    scope_html = f"<span>{esc(scope)}</span>" if scope else ""
    return (
        "<section class='panel'>"
        "<div class='meta'>"
        "<span class='badge'>Cached page</span>"
        f"<span>{esc(message)}</span>"
        f"{scope_html}"
        "<button class='button' id='refresh-now' type='button'>Refresh now</button>"
        "</div>"
        "</section>"
    )


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
      <a class="button" href="/{esc(kind if kind != 'all' else '')}">Clear</a>
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
      <p class="muted" id="watch-status">No browser watch configured.</p>
    </section>
    """


def render_process_filters(ui: dict[str, object]) -> str:
    q = str(ui.get("q") or "")
    server = str(ui.get("server") or "")
    health = str(ui.get("health") or "")
    return f"""
    <form class="filters" method="get">
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
      <a class="button" href="/me">Clear</a>
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
      <label>Search <input name="q" value="{esc(q)}" placeholder="alias, model, user"></label>
      <label>Tag <input name="tag" value="{esc(tag)}" placeholder="A100"></label>
      <label><input type="checkbox" name="online" value="1" {checked(online)}> Online only</label>
      <label><input type="checkbox" name="free" value="1" {checked(free)}> Has free GPU</label>
      <label><input type="checkbox" name="alerts" value="1" {checked(alerts)}> Has alerts</label>
      <label><input type="checkbox" name="mine" value="1" {checked(mine)}> Has my processes</label>
      <button class="button" type="submit">Filter</button>
      <a class="button" href="/servers">Clear</a>
    </form>
    """


def render_alert_filters(ui: dict[str, object]) -> str:
    severity = str(ui.get("severity") or "")
    q = str(ui.get("q") or "")
    status = str(ui.get("alert_status") or "active")
    return f"""
    <form class="filters" method="get">
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
      <a class="button" href="/alerts">Clear</a>
    </form>
    """


def render_train_now(overview: dict[str, object], *, limit: int | None = None) -> str:
    ui = {"availability": "available"}
    items = filter_gpu_items(overview.get("gpu_items") or [], ui)
    if limit is not None:
        items = items[:limit]
    if not items:
        return render_available_gpus([], ui, title="Train Now / Recommended GPUs", counts=overview)
    cards = "".join(render_gpu_recommendation_card(item) for item in items)
    return (
        "<section class='panel'>"
        "<div class='section-head'><h2>Train Now / Recommended GPUs</h2><a href='/gpus'>View all</a></div>"
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


def render_failure_inbox(items: object, *, limit: int | None = None) -> str:
    rows = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return "<section class='panel'><div class='section-head'><h2>Failed or Suspicious Runs</h2></div><p class='muted'>No failed run, suspected idle process, or failure signal found.</p></section>"
    body = "".join(
        f"<tr><td>{esc(item.get('source') or item.get('kind') or '-')}</td><td>{esc(item.get('name') or '-')}</td><td>{esc(item.get('host') or '-')}</td><td>{esc(item.get('gpu') or '-')}</td><td>{esc(item.get('pid') or '-')}</td><td><span class='badge {esc(health_badge(item.get('status')))}'>{esc(item.get('status') or '-')}</span></td><td>{esc(short(item.get('diagnosis') or '-', 120))}</td><td>{render_training_actions(item)}</td></tr>"
        for item in rows
    )
    return f"<section class='panel'><div class='section-head'><h2>Failed or Suspicious Runs</h2><a href='/me'>My training</a></div><table><tr><th>Source</th><th>Name</th><th>Host</th><th>GPU</th><th>PID</th><th>Status</th><th>Signal</th><th>Action</th></tr>{body}</table></section>"


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
        return (
            f"<section class='panel'>{head}"
            "<p><strong>No clearly free GPU found.</strong></p>"
            f"<p class='muted'>{esc(busy)} GPUs are busy.</p>"
            f"<p class='muted'>{esc(idle)} GPUs look idle but occupied.</p>"
            "<div class='actions empty-actions'>"
            "<a class='button' href='/gpus?availability=busy'>View busy GPUs</a>"
            "<a class='button' href='/gpus?availability=idle'>View suspected idle GPUs</a>"
            f"<a class='button' href='/gpus?availability=all'>View all GPUs ({esc(total)})</a>"
            "</div></section>"
        )
    rows = "".join(
        f"<tr><td><a href='/servers/{esc(item.get('server'))}'>{esc(item.get('server'))}</a></td><td>GPU {esc(item.get('gpu_index'))}</td><td>{esc(short(item.get('name') or '', 34))}</td><td>{esc(item.get('memory_free_mb'))} MB</td><td>{esc(item.get('utilization_gpu'))}%</td><td>{esc(item.get('temperature'))} C</td><td>{esc(item.get('disk_health'))}</td><td><code>{esc(item.get('ssh_command'))}</code><br><code>CUDA_VISIBLE_DEVICES={esc(item.get('cuda_visible_devices'))}</code><br>{render_open_ssh_button(item.get('server'))}</td></tr>"
        for item in items
        if isinstance(item, dict)
    )
    return f"<section class='panel'>{head}<table><tr><th>Server</th><th>GPU</th><th>Model</th><th>Free memory</th><th>Util</th><th>Temp</th><th>Disk</th><th>Copy</th></tr>{rows}</table></section>"


def render_gpu_finder(overview: dict[str, object], ui: dict[str, object]) -> str:
    items = filter_gpu_items(overview.get("gpu_items") or [], ui)
    if not items:
        return render_available_gpus([], ui, title="Train Now / Recommended GPUs", counts=overview)
    cards = "".join(render_gpu_recommendation_card(item) for item in items)
    return f"<section class='panel'><div class='section-head'><h2>Train Now Recommendations</h2><a href='/gpus?availability=all'>View all</a></div><div class='gpu-list'>{cards}</div></section>"


def render_gpu_recommendation_card(item: dict[str, object]) -> str:
    rec = gpu_recommendation(item)
    reasons = ranking.gpu_recommendation_reasons(item, rec)
    reason_items = "".join(f"<li>{esc(reason)}</li>" for reason in reasons)
    memory_free = format_memory(item.get("memory_free_mb"))
    memory_total = format_memory(item.get("memory_total_mb"))
    ssh_command = str(item.get("ssh_command") or ("ssh " + str(item.get("server") or "")))
    cuda = str(item.get("cuda_visible_devices") or item.get("index") or "")
    snippet = ranking.launch_snippet(item)
    open_terminal = render_open_ssh_button(item.get("server"))
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


def render_open_ssh_button(server: object) -> str:
    if not ServerHandler.action_allowed or ServerHandler.fake_lab or not server:
        return ""
    return f'<button class="small" type="button" data-open-ssh="{esc(server)}">Open SSH terminal</button>'


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
    if not host:
        content = "<p class='muted'>Server not found.</p>"
    else:
        host = display_with_cache(host)
        content = f"""
        <section class="toolbar">
          <div>
            <h1>{esc(host.get('alias'))}</h1>
            <p>{esc(host.get('remote_hostname') or host.get('hostname') or '')}</p>
          </div>
          <div class="actions">
            <button class="button" id="pause-refresh" type="button">Pause refresh</button>
            <a class="button" href="/">All servers</a>
            <a class="button" href="/api/servers/{esc(host.get('alias'))}">JSON</a>
          </div>
        </section>
        {render_data_status(data)}
        {render_cache_notice(host)}
        {render_health(host)}
        {render_labgpu_runs(host)}
        <section class="panel"><h2>Disks</h2>{render_disk_table(host.get('disks') or [])}</section>
        <section class="panel"><h2>GPUs</h2><div class="gpu-grid">{''.join(render_gpu_card(gpu, server_alias=host.get('alias')) for gpu in host.get('gpus') or [])}</div></section>
        <section class="panel"><h2>Processes</h2>{render_process_table(host.get('processes') or [], server_alias=host.get('alias'))}</section>
        """
    return page("LabGPU Server", content)


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


def page(title: str, body: str) -> str:
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
.topnav{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:18px}}
.topnav a,.topnav button{{border:1px solid var(--border);background:var(--button);border-radius:999px;padding:6px 10px;text-decoration:none;color:var(--link);font:inherit;cursor:pointer}}
dialog{{border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);max-width:min(560px,calc(100vw - 32px));padding:18px}}
dialog::backdrop{{background:rgba(0,0,0,.45)}}
.modal-actions{{display:flex;gap:8px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap}}
.toolbar{{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:18px}}
.actions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.button{{border:1px solid var(--border);background:var(--button);border-radius:6px;padding:7px 10px;color:var(--text);text-decoration:none;cursor:pointer}}
.filters{{display:flex;gap:10px;align-items:end;flex-wrap:wrap;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:14px}}
.filters label{{display:flex;flex-direction:column;gap:4px;color:var(--muted);font-size:12px}}
.filters input,.filters select{{border:1px solid var(--border);border-radius:6px;padding:7px 8px;min-width:150px;background:var(--button);color:var(--text)}}
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
html[data-theme="dark"] .warn-text{{color:#facc15}}
html[data-theme="dark"] .danger{{color:#fca5a5;border-color:#7f1d1d}}
@media(max-width:640px){{main{{width:calc(100vw - 20px)}}.grid,.split{{grid-template-columns:1fr}}.toolbar{{align-items:flex-start;flex-direction:column}}.assistant-form{{grid-template-columns:1fr}}}}
</style></head><body><main>{render_nav()}{body}</main>
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
<script>
let paused = false;
const actionToken = "{esc(ServerHandler.action_token)}";
let selectedStopButton = null;
const themeButton = document.getElementById("theme-toggle");
const languageButton = document.getElementById("language-toggle");
const translations = {{
  "Overview": "总览",
  "Train Now": "现在开跑",
  "My Training": "我的训练",
  "Assistant": "助手",
  "Servers": "服务器",
  "Alerts": "告警",
  "Settings": "设置",
  "JSON": "JSON",
  "Pause refresh": "暂停刷新",
  "Resume refresh": "继续刷新",
  "Refresh now": "立即刷新",
  "Cached page": "缓存页面",
  "Dark": "深色",
  "Light": "浅色",
  "LabGPU Home": "LabGPU 主页",
  "LabGPU Assistant": "LabGPU 助手",
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
  "Copy SSH command": "复制 SSH 命令",
  "Copy CUDA_VISIBLE_DEVICES": "复制 CUDA_VISIBLE_DEVICES",
  "Copy launch snippet": "复制启动片段",
  "Open SSH terminal": "打开 SSH 终端",
  "Opening terminal...": "正在打开终端...",
  "Terminal opened": "终端已打开",
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
try {{
  applyTheme(localStorage.getItem("labgpu-theme") || "system");
  applyLanguage(currentLanguage());
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
}} catch (error) {{}}
const btn = document.getElementById("pause-refresh");
if (btn) {{
  btn.addEventListener("click", () => {{
    paused = !paused;
    btn.textContent = translateText(paused ? "Resume refresh" : "Pause refresh", currentLanguage());
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
document.addEventListener("click", async (event) => {{
  const target = event.target;
  const button = target && target.closest ? target.closest("[data-open-ssh]") : null;
  if (!button) return;
  const original = button.textContent || "Open SSH terminal";
  button.textContent = translateText("Opening terminal...", currentLanguage());
  button.disabled = true;
  const response = await fetch(`/api/servers/${{encodeURIComponent(button.dataset.openSsh || "")}}/open-ssh`, {{
    method: "POST",
    headers: {{"X-LabGPU-Action-Token": actionToken}}
  }});
  const payload = await response.json().catch(() => ({{ok: false, message: "Opening terminal failed."}}));
  button.disabled = false;
  if (payload.ok) {{
    button.textContent = translateText("Terminal opened", currentLanguage());
    setTimeout(() => button.textContent = original, 1400);
  }} else {{
    button.textContent = original;
    window.alert(payload.message || "Opening terminal failed.");
  }}
}});
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
const assistantForm = document.getElementById("assistant-form");
const assistantInput = document.getElementById("assistant-input");
const assistantChat = document.getElementById("assistant-chat");
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
    body: JSON.stringify({{message}})
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
    const message = `${{hit.dataset.server}} GPU ${{hit.dataset.gpuIndex}} is available`;
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


def render_nav() -> str:
    return """
    <nav class="topnav">
      <a href="/">Overview</a>
      <a href="/gpus">Train Now</a>
      <a href="/me">My Training</a>
      <a href="/assistant">Assistant</a>
      <a href="/servers">Servers</a>
      <a href="/alerts">Alerts</a>
      <a href="/settings">Settings</a>
      <button id="language-toggle" type="button">中文</button>
      <button id="theme-toggle" type="button">Dark</button>
    </nav>
    """


def section_head(title: str, view_all: str | None = None) -> str:
    link = f"<a href='{esc(view_all)}'>View all</a>" if view_all else ""
    return f"<div class='section-head'><h2>{esc(title)}</h2>{link}</div>"


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
