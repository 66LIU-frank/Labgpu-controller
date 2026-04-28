from __future__ import annotations

import html
import json
import secrets
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from labgpu.core.config import load_config
from labgpu.remote.actions import stop_process
from labgpu.remote.cache import read_server_cache, write_server_cache
from labgpu.remote.inventory import load_inventory
from labgpu.remote.probe import probe_host
from labgpu.remote.ssh_config import SSHHost, parse_ssh_config, resolve_ssh_host
from labgpu.remote.state import annotate_server, build_overview, human_duration


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
) -> None:
    ServerHandler.ssh_config = ssh_config
    ServerHandler.names = names
    ServerHandler.pattern = pattern
    ServerHandler.timeout = timeout
    ServerHandler.action_allowed = is_loopback(host) or allow_actions
    ServerHandler.action_token = secrets.token_urlsafe(24)
    if host == "0.0.0.0":
        print("Warning: LabGPU servers dashboard has no authentication in this version.")
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
) -> dict[str, object]:
    hosts = load_inventory(ssh_config=ssh_config, names=names, pattern=pattern)
    if not hosts:
        return {"hosts": [], "count": 0, "error": "no SSH hosts selected"}
    hosts = [resolve_ssh_host(host) for host in hosts]
    results = []
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as executor:
        futures = {executor.submit(probe_host, host, timeout=timeout): host for host in hosts}
        for future in as_completed(futures):
            result = future.result()
            result = annotate_server(result)
            if result.get("online"):
                write_server_cache(result)
            else:
                cached = read_server_cache(str(result.get("alias") or futures[future].alias))
                if cached:
                    result["cached"] = cached
                    result["last_seen"] = cached.get("probed_at")
            results.append(result)
    results.sort(key=lambda item: str(item.get("alias")))
    return {"hosts": results, "count": len(results), "overview": build_overview(results), "error": None}


class ServerHandler(BaseHTTPRequestHandler):
    ssh_config: str | Path | None = None
    names: list[str] | None = None
    pattern: str | None = None
    timeout: int = 8
    action_allowed: bool = False
    action_token: str = ""

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
        elif parsed.path == "/api/overview":
            self._json(self._data(parsed.query))
        elif parsed.path == "/api/servers":
            self._json(self._data(parsed.query))
        elif parsed.path.startswith("/servers/"):
            alias = unquote(parsed.path.removeprefix("/servers/")).strip("/")
            self._html(render_detail(self._data_for_alias(alias)))
        elif parsed.path.startswith("/api/servers/"):
            alias = unquote(parsed.path.removeprefix("/api/servers/")).strip("/")
            self._json(self._data_for_alias(alias))
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
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _data(self, query: str) -> dict[str, object]:
        params = parse_qs(query)
        names = self.names
        if params.get("hosts"):
            names = split_hosts(params["hosts"][0])
        pattern = params.get("pattern", [self.pattern])[0]
        data = collect_servers(
            ssh_config=self.ssh_config,
            names=names,
            pattern=pattern,
            timeout=self.timeout,
        )
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
        }
        return data

    def _data_for_alias(self, alias: str) -> dict[str, object]:
        return collect_servers(
            ssh_config=self.ssh_config,
            names=[alias],
            pattern=None,
            timeout=self.timeout,
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
    cards = "".join(render_host_card(host, compact=True) for host in preview_list(hosts, 6))
    if not cards:
        cards = f"<p class='muted'>{esc(data.get('error') or 'No hosts found.')}</p>"
    return page(
        "LabGPU Home",
        f"""
        <section class="toolbar">
          <div>
            <h1>LabGPU Home</h1>
            <p>Local SSH dashboard for lab GPU machines</p>
          </div>
          <div class="actions">
            <button class="button" id="pause-refresh" type="button">Pause refresh</button>
            <a class="button" href="/api/servers">JSON</a>
          </div>
        </section>
        {render_overview(overview)}
        <div class="split">
          {render_available_gpus(overview.get('available_gpu_items') if isinstance(overview, dict) else [], {}, limit=6, title='Available GPUs', view_all='/gpus')}
          {render_my_processes(overview.get('my_process_items') if isinstance(overview, dict) else [], limit=6, view_all='/me')}
        </div>
        <div class="split">
          {render_alerts(overview.get('alert_items') if isinstance(overview, dict) else [], limit=8, view_all='/alerts')}
          <section class="panel"><div class="section-head"><h2>Servers</h2><a href="/servers">View all</a></div><div class="grid compact">{cards}</div></section>
        </div>
        """,
    )


def render_gpus_page(data: dict[str, object]) -> str:
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    return page(
        "Find GPUs",
        f"""
        <section class="toolbar">
          <div>
            <h1>Find GPUs</h1>
            <p>Find the best free GPU across your SSH servers.</p>
          </div>
          <div class="actions"><button class="button" id="pause-refresh" type="button">Pause refresh</button><a class="button" href="/">Overview</a></div>
        </section>
        {render_filters(ui, kind='gpus')}
        {render_available_gpus(overview.get('available_gpu_items') if isinstance(overview, dict) else [], ui, title='Available GPUs')}
        """,
    )


def render_me_page(data: dict[str, object]) -> str:
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    return page(
        "My Processes",
        f"""
        <section class="toolbar">
          <div>
            <h1>My Processes</h1>
            <p>Your GPU processes across all configured servers.</p>
          </div>
          <div class="actions"><button class="button" id="pause-refresh" type="button">Pause refresh</button><a class="button" href="/">Overview</a></div>
        </section>
        {render_process_filters(ui)}
        {render_my_processes(overview.get('my_process_items') if isinstance(overview, dict) else [], ui=ui, title='My GPU Processes')}
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
        {render_alert_filters(ui)}
        {render_alerts(overview.get('alert_items') if isinstance(overview, dict) else [], ui=ui, title='All Alerts')}
        """,
    )


def render_settings_page(*, ssh_config: str | Path | None = None) -> str:
    config = load_config()
    ssh_hosts = parse_ssh_config(ssh_config)
    host_rows = "".join(
        f"<tr><td><code>{esc(host.alias)}</code></td><td>{esc(host.hostname or '-')}</td><td>{esc(host.user or '-')}</td><td>{esc(host.port or 22)}</td><td><code>labgpu servers import-ssh --hosts {esc(host.alias)}</code></td></tr>"
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
          <p class="muted">Alpha settings are intentionally simple: choose aliases here, then use the import command. Full in-page editing can be added after the server model settles.</p>
          <table><tr><th>Alias</th><th>HostName</th><th>User</th><th>Port</th><th>Import command</th></tr>{host_rows}</table>
        </section>
        """,
    )


def render_overview(overview: dict[str, object]) -> str:
    return f"""
    <section class="health">
      <div><strong>{esc(overview.get('online_servers', 0))}/{esc(overview.get('total_servers', 0))}</strong><span>online servers</span></div>
      <div><strong>{esc(overview.get('available_gpus', 0))}/{esc(overview.get('total_gpus', 0))}</strong><span>available GPUs</span></div>
      <div><strong>{esc(overview.get('my_processes', 0))}</strong><span>my GPU processes</span></div>
      <div><strong>{esc(overview.get('alerts', 0))}</strong><span>alerts</span></div>
    </section>
    """


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
      <button class="button" type="submit">Filter</button>
      <a class="button" href="/alerts">Clear</a>
    </form>
    """


def render_available_gpus(
    items: object,
    ui: dict[str, object] | None = None,
    *,
    title: str = "Available GPUs",
    limit: int | None = None,
    view_all: str | None = None,
) -> str:
    items = filter_available_gpu_items(items, ui or {})
    if limit is not None:
        items = items[:limit]
    head = section_head(title, view_all)
    if not isinstance(items, list) or not items:
        return f"<section class='panel'>{head}<p class='muted'>No clearly free GPU found.</p></section>"
    rows = "".join(
        f"<tr><td><a href='/servers/{esc(item.get('server'))}'>{esc(item.get('server'))}</a></td><td>GPU {esc(item.get('gpu_index'))}</td><td>{esc(short(item.get('name') or '', 34))}</td><td>{esc(item.get('memory_free_mb'))} MB</td><td>{esc(item.get('utilization_gpu'))}%</td><td>{esc(item.get('temperature'))} C</td><td>{esc(item.get('disk_health'))}</td><td><code>{esc(item.get('ssh_command'))}</code><br><code>CUDA_VISIBLE_DEVICES={esc(item.get('cuda_visible_devices'))}</code></td></tr>"
        for item in items
        if isinstance(item, dict)
    )
    return f"<section class='panel'>{head}<table><tr><th>Server</th><th>GPU</th><th>Model</th><th>Free memory</th><th>Util</th><th>Temp</th><th>Disk</th><th>Copy</th></tr>{rows}</table></section>"


def filter_available_gpu_items(items: object, ui: dict[str, object]) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    q = str(ui.get("q") or "").lower()
    model = str(ui.get("model") or "").lower()
    tag = str(ui.get("tag") or "").lower()
    sort = str(ui.get("sort") or "")
    min_mem_raw = str(ui.get("min_mem_gb") or "").strip()
    min_mem_mb = None
    if min_mem_raw:
        try:
            min_mem_mb = int(float(min_mem_raw) * 1024)
        except ValueError:
            min_mem_mb = None
    filtered: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        haystack = " ".join(
            [
                str(item.get("server") or ""),
                str(item.get("name") or ""),
                join_values(item.get("tags") or []),
            ]
        ).lower()
        if q and q not in haystack:
            continue
        if model and model not in str(item.get("name") or "").lower():
            continue
        if tag and tag not in join_values(item.get("tags") or []).lower():
            continue
        if min_mem_mb is not None and (item.get("memory_free_mb") or 0) < min_mem_mb:
            continue
        filtered.append(item)
    if sort == "model":
        filtered.sort(key=lambda item: (str(item.get("name") or ""), str(item.get("server") or "")))
    elif sort == "load":
        filtered.sort(key=lambda item: load_sort_key(item.get("load")))
    else:
        filtered.sort(key=lambda item: int(item.get("memory_free_mb") or 0), reverse=True)
    return filtered


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
    filtered: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        haystack = " ".join([str(item.get("server") or ""), str(item.get("type") or ""), str(item.get("message") or "")]).lower()
        if q and q not in haystack:
            continue
        if severity and severity != str(item.get("severity") or "").lower():
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
    title: str = "My Processes",
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
        f"<tr><td><a href='/servers/{esc(item.get('server'))}'>{esc(item.get('server'))}</a></td><td>{esc(item.get('type') or '-')}</td><td><span class='badge {esc(item.get('severity'))}'>{esc(item.get('severity'))}</span></td><td>{esc(item.get('message'))}</td><td><button class='small' type='button' disabled>Dismiss</button> <button class='small' type='button' disabled>Snooze</button></td></tr>"
        for item in items
        if isinstance(item, dict)
    )
    return f"<section class='panel'>{head}<table><tr><th>Server</th><th>Type</th><th>Severity</th><th>Message</th><th>Action</th></tr>{rows}</table></section>"


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
    status = "online" if online else "offline"
    error = host.get("error")
    gpus = host.get("gpus") or []
    cached = host.get("cached") if isinstance(host.get("cached"), dict) else None
    display_gpus = gpus or (cached.get("gpus") if cached else []) or []
    disk = host.get("disk") or {}
    if not disk and cached:
        disk = cached.get("disk") or {}
    gpu_rows = "".join(render_gpu_row(gpu) for gpu in display_gpus) or "<tr><td colspan='5' class='muted'>No GPU data.</td></tr>"
    summary = gpu_summary(display_gpus)
    memory = host.get("memory") if isinstance(host.get("memory"), dict) else {}
    mem = memory.get("mem") if isinstance(memory.get("mem"), dict) else {}
    mode = host.get("mode") or "offline"
    href = f"/servers/{esc(host.get('alias'))}"
    gpu_table = "" if compact else f"""
      <table>
        <tr><th>GPU</th><th>Name</th><th>Memory</th><th>Util</th><th>Processes</th></tr>
        {gpu_rows}
      </table>
    """
    alerts = host.get("alerts") or []
    return f"""
    <article class="card">
      <div class="card-head">
        <div>
          <h2><a href="{href}">{esc(host.get('alias'))}</a></h2>
          <p>{esc(host.get('remote_hostname') or host.get('hostname') or '')}</p>
        </div>
        <span class="pill {status}">{status}</span>
      </div>
      <div class="meta">
        <span>user {esc(host.get('user') or '-')}</span>
        <span>port {esc(host.get('port') or '22')}</span>
        <span>{esc(mode)}</span>
        <span>{esc(join_values(host.get('tags') or []))}</span>
        <span>{esc(host.get('elapsed_ms'))} ms</span>
      </div>
      <div class="meta">
        <span>{esc(summary['total'])} GPUs</span>
        <span>{esc(summary['free'])} free / {esc(summary['busy'])} busy</span>
        <span>load {esc(load_label(host))}</span>
        <span>mem {esc(mem.get('used_percent') if isinstance(mem, dict) else '-')}%</span>
        <span>disk {esc(disk.get('use_percent') if isinstance(disk, dict) else '-')}</span>
      </div>
      <div class="meta">
        <span>{esc(host.get('uptime') or '-')}</span>
        <span>top users {esc(top_users(host.get('processes') or []))}</span>
        <span>alerts {esc(len(alerts) if isinstance(alerts, list) else 0)}</span>
        <span>last probe {esc(host.get('probed_at') or '-')}</span>
      </div>
      {f"<p class='muted'>offline, last seen {esc(host.get('last_seen'))}</p>" if cached and not online else ""}
      {f"<p class='error'>{esc(error)}</p>" if error else ""}
      {gpu_table}
    </article>
    """


def display_with_cache(host: dict[str, object]) -> dict[str, object]:
    cached = host.get("cached")
    if host.get("online") or not isinstance(cached, dict):
        return host
    merged = dict(cached)
    for key in ("alias", "hostname", "user", "port", "proxyjump", "online", "error", "elapsed_ms", "mode", "last_seen", "cached"):
        if key in host:
            merged[key] = host[key]
    return merged


def render_gpu_row(gpu: object) -> str:
    if not isinstance(gpu, dict):
        return ""
    processes = gpu.get("processes") or []
    process_text = "<br>".join(
        f"{esc(proc.get('user') or '?')} pid {esc(proc.get('pid'))} {esc(proc.get('used_memory_mb'))}MB {esc(short(proc.get('command') or '', 80))}"
        for proc in processes
        if isinstance(proc, dict)
    ) or "<span class='muted'>free</span>"
    return (
        f"<tr><td>{esc(gpu.get('index'))}</td>"
        f"<td>{esc(short(gpu.get('name') or '', 22))}</td>"
        f"<td>{esc(gpu.get('memory_used_mb'))}/{esc(gpu.get('memory_total_mb'))} MB</td>"
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
      <div><strong>{esc(host.get('probed_at') or '-')}</strong><span>last probe</span></div>
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
        <span>{esc(gpu.get('memory_used_mb'))}/{esc(gpu.get('memory_total_mb'))} MB</span>
        <span>{esc(gpu.get('utilization_gpu'))}% util</span>
        <span>{esc(gpu.get('temperature'))} C</span>
      </div>
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
    command = short(proc.get("command") or "", 140)
    gpu_value = proc.get("gpu_index") if proc.get("gpu_index") is not None else short(proc.get("gpu_uuid") or "", 14)
    gpu = f"<td>{esc(gpu_value)}</td>" if include_gpu else ""
    server = f"<td>{esc(server_alias)}</td>" if show_server and server_alias is not None else ""
    state = f"<td>{esc(proc.get('state') or '-')}</td>" if include_gpu else ""
    health_class = proc.get("health_severity") or proc.get("health_status") or "unknown"
    health = f"<span class='badge {esc(health_class)}'>{esc(proc.get('health_status') or 'unknown')}</span>"
    action = render_process_action(proc, server_alias=server_alias, action_allowed=action_allowed)
    return (
        f"<tr>{server}{gpu}<td>{esc(proc.get('user') or '?')}</td><td>{esc(proc.get('pid'))}</td>"
        f"<td>{esc(proc.get('runtime') or human_duration(proc.get('runtime_seconds')))}</td>{state}"
        f"<td>{esc(proc.get('used_memory_mb'))} MB</td><td title='{esc(proc.get('health_reason') or '')}'>{health}</td>"
        f"<td><code>{esc(command)}</code></td><td>{action}</td></tr>"
    )


def render_process_action(proc: dict[str, object], *, server_alias: object | None, action_allowed: bool) -> str:
    if proc.get("actions_disabled_reason"):
        return f"<span class='muted'>{esc(proc.get('actions_disabled_reason'))}</span>"
    if proc.get("is_current_user") and action_allowed and server_alias:
        return (
            f"<button class='small danger' data-stop='term' data-server='{esc(server_alias)}' data-pid='{esc(proc.get('pid'))}' "
            f"data-user='{esc(proc.get('user') or '')}' data-start='{esc(proc.get('start_time') or '')}' "
            f"data-hash='{esc(proc.get('command_hash') or '')}' data-command='{esc(short(proc.get('command') or '', 180))}'>Stop</button>"
        )
    if proc.get("is_current_user") and not action_allowed:
        return "<span class='muted'>actions disabled</span>"
    return f"<code>labgpu adopt {esc(proc.get('pid'))} --name NAME</code>"


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
.section-head{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:4px}}
.section-head a{{font-size:13px;color:var(--link)}}
.health{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}}
.health>div{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px}}
.health strong{{display:block;font-size:18px}} .health span{{color:var(--muted);font-size:12px}}
.gpu-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px;margin-top:10px}}
.gpu-card{{border:1px solid var(--border-soft);border-radius:8px;padding:12px;background:var(--surface-soft);overflow:hidden}}
.gpu-card h3 span{{color:var(--muted);font-weight:500}}
table{{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}}
th,td{{border-top:1px solid var(--row);padding:7px;text-align:left;vertical-align:top}} th{{color:var(--muted)}}
html[data-theme="dark"] .pill.online{{color:#86efac;background:#143421}} html[data-theme="dark"] .pill.offline{{color:#fca5a5;background:#3a1717}}
html[data-theme="dark"] .badge.ok{{background:#143421;color:#86efac}} html[data-theme="dark"] .badge.warning{{background:#3b2a0a;color:#facc15}} html[data-theme="dark"] .badge.error{{background:#3a1717;color:#fca5a5}}
html[data-theme="dark"] .danger{{color:#fca5a5;border-color:#7f1d1d}}
@media(max-width:640px){{main{{width:calc(100vw - 20px)}}.grid,.split{{grid-template-columns:1fr}}.toolbar{{align-items:flex-start;flex-direction:column}}}}
</style></head><body><main>{render_nav()}{body}</main>
<script>
let paused = false;
const actionToken = "{esc(ServerHandler.action_token)}";
const themeButton = document.getElementById("theme-toggle");
function applyTheme(theme) {{
  const dark = theme === "dark" || (theme === "system" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  if (themeButton) themeButton.textContent = dark ? "Light mode" : "Dark mode";
}}
try {{
  applyTheme(localStorage.getItem("labgpu-theme") || "system");
  if (themeButton) {{
    themeButton.addEventListener("click", () => {{
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      localStorage.setItem("labgpu-theme", next);
      applyTheme(next);
    }});
  }}
}} catch (error) {{}}
const btn = document.getElementById("pause-refresh");
if (btn) {{
  btn.addEventListener("click", () => {{
    paused = !paused;
    btn.textContent = paused ? "Resume refresh" : "Pause refresh";
  }});
}}
setInterval(() => {{
  if (!paused) window.location.reload();
}}, 15000);
document.querySelectorAll("[data-stop]").forEach((button) => {{
  button.addEventListener("click", async () => {{
    const msg = `Stop this process?\\n\\nServer: ${{button.dataset.server}}\\nPID: ${{button.dataset.pid}}\\nUser: ${{button.dataset.user}}\\nCommand: ${{button.dataset.command || "-"}}\\n\\nThis sends SIGTERM first.`;
    if (!window.confirm(msg)) return;
    let payload = await stopProcess(button, false);
    if (!payload.ok && payload.result === "alive" && window.confirm("Process is still alive. Force kill with SIGKILL?")) {{
      payload = await stopProcess(button, true);
    }}
    window.alert(payload.message || payload.result || "done");
    if (payload.ok) window.location.reload();
  }});
}});
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


def render_nav() -> str:
    return """
    <nav class="topnav">
      <a href="/">Overview</a>
      <a href="/gpus">Find GPUs</a>
      <a href="/me">My Processes</a>
      <a href="/servers">Servers</a>
      <a href="/alerts">Alerts</a>
      <a href="/settings">Settings</a>
      <button id="theme-toggle" type="button">Dark mode</button>
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
    if isinstance(value, dict):
        raw = value.get("1m")
        try:
            return (float(raw), "")
        except (TypeError, ValueError):
            pass
    return (999999.0, "")


def alert_rank(value: object) -> int:
    return {"error": 0, "warning": 1, "info": 2}.get(str(value), 3)


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
        user = str(proc.get("user") or "?")
        counts[user] = counts.get(user, 0) + 1
    if not counts:
        return "-"
    return ", ".join(name for name, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3])


def join_values(values: object) -> str:
    if not isinstance(values, list):
        return "-"
    return ",".join(str(value) for value in values) or "-"


def is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}
