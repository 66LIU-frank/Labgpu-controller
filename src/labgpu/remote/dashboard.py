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

from labgpu.remote.actions import stop_process
from labgpu.remote.cache import read_server_cache, write_server_cache
from labgpu.remote.inventory import load_inventory
from labgpu.remote.probe import probe_host
from labgpu.remote.ssh_config import SSHHost, resolve_ssh_host
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
    cards = "".join(render_host_card(host) for host in hosts)
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
        {render_filters(data.get('ui') if isinstance(data.get('ui'), dict) else {})}
        {render_overview(overview)}
        {render_available_gpus(overview.get('available_gpu_items') if isinstance(overview, dict) else [], data.get('ui') if isinstance(data.get('ui'), dict) else {})}
        {render_my_processes(overview.get('my_process_items') if isinstance(overview, dict) else [])}
        {render_alerts(overview.get('alert_items') if isinstance(overview, dict) else [])}
        <section class="grid">{cards}</section>
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


def render_filters(ui: dict[str, object]) -> str:
    q = str(ui.get("q") or "")
    model = str(ui.get("model") or "")
    min_mem = str(ui.get("min_mem_gb") or "")
    return f"""
    <form class="filters" method="get">
      <label>Search <input name="q" value="{esc(q)}" placeholder="alpha, A100, lsg"></label>
      <label>Model <input name="model" value="{esc(model)}" placeholder="A100 / 4090 / H800"></label>
      <label>Min free GB <input name="min_mem_gb" value="{esc(min_mem)}" placeholder="24"></label>
      <button class="button" type="submit">Filter</button>
      <a class="button" href="/">Clear</a>
    </form>
    """


def render_available_gpus(items: object, ui: dict[str, object] | None = None) -> str:
    items = filter_available_gpu_items(items, ui or {})
    if not isinstance(items, list) or not items:
        return "<section class='panel'><h2>Available GPUs</h2><p class='muted'>No clearly free GPU found.</p></section>"
    rows = "".join(
        f"<tr><td>{esc(item.get('server'))}</td><td>GPU {esc(item.get('gpu_index'))}</td><td>{esc(short(item.get('name') or '', 34))}</td><td>{esc(item.get('memory_free_mb'))} MB</td><td>{esc(item.get('utilization_gpu'))}%</td><td><code>CUDA_VISIBLE_DEVICES={esc(item.get('cuda_visible_devices'))}</code></td></tr>"
        for item in items
        if isinstance(item, dict)
    )
    return f"<section class='panel'><h2>Available GPUs</h2><table><tr><th>Server</th><th>GPU</th><th>Model</th><th>Free memory</th><th>Util</th><th>Copy</th></tr>{rows}</table></section>"


def filter_available_gpu_items(items: object, ui: dict[str, object]) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    q = str(ui.get("q") or "").lower()
    model = str(ui.get("model") or "").lower()
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
        if min_mem_mb is not None and (item.get("memory_free_mb") or 0) < min_mem_mb:
            continue
        filtered.append(item)
    return filtered


def render_my_processes(items: object) -> str:
    if not isinstance(items, list) or not items:
        return "<section class='panel'><h2>My Processes</h2><p class='muted'>No GPU process owned by the SSH user.</p></section>"
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
    return f"<section class='panel'><h2>My Processes</h2><table><tr><th>Server</th><th>GPU</th><th>User</th><th>PID</th><th>Runtime</th><th>State</th><th>Memory</th><th>Health</th><th>Command</th><th>Action</th></tr>{rows}</table></section>"


def render_alerts(items: object) -> str:
    if not isinstance(items, list) or not items:
        return "<section class='panel'><h2>Alerts</h2><p class='muted'>No current alerts.</p></section>"
    rows = "".join(
        f"<tr><td>{esc(item.get('server'))}</td><td><span class='badge {esc(item.get('severity'))}'>{esc(item.get('severity'))}</span></td><td>{esc(item.get('message'))}</td></tr>"
        for item in items
        if isinstance(item, dict)
    )
    return f"<section class='panel'><h2>Alerts</h2><table><tr><th>Server</th><th>Severity</th><th>Message</th></tr>{rows}</table></section>"


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


def render_host_card(host: object) -> str:
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
        <span>last probe {esc(host.get('probed_at') or '-')}</span>
      </div>
      {f"<p class='muted'>offline, last seen {esc(host.get('last_seen'))}</p>" if cached and not online else ""}
      {f"<p class='error'>{esc(error)}</p>" if error else ""}
      <table>
        <tr><th>GPU</th><th>Name</th><th>Memory</th><th>Util</th><th>Processes</th></tr>
        {gpu_rows}
      </table>
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
    health = f"<span class='badge {esc(proc.get('health_status') or 'unknown')}'>{esc(proc.get('health_status') or 'unknown')}</span>"
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
<style>
body{{font:14px/1.45 system-ui,sans-serif;margin:0;background:#f7f7f4;color:#1f2328}}
main{{width:min(1280px,calc(100vw - 32px));margin:0 auto;padding:22px 0 36px}}
h1,h2,h3,p{{margin:0}} h1{{font-size:28px}} h2{{font-size:18px}} h3{{font-size:15px}} p,.muted{{color:#667085}}
a{{color:inherit}} code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}}
.toolbar{{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:18px}}
.actions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.button{{border:1px solid #d0d5dd;background:#fff;border-radius:6px;padding:7px 10px;color:#1f2328;text-decoration:none;cursor:pointer}}
.filters{{display:flex;gap:10px;align-items:end;flex-wrap:wrap;background:#fff;border:1px solid #d8d8d0;border-radius:8px;padding:12px;margin-bottom:14px}}
.filters label{{display:flex;flex-direction:column;gap:4px;color:#667085;font-size:12px}}
.filters input{{border:1px solid #d0d5dd;border-radius:6px;padding:7px 8px;min-width:150px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}}
.card{{background:#fff;border:1px solid #d8d8d0;border-radius:8px;padding:14px;overflow:hidden}}
.card-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px}}
.meta{{display:flex;flex-wrap:wrap;gap:10px;margin:6px 0;color:#667085;font-size:13px}}
.pill{{border-radius:999px;padding:2px 9px;font-size:12px;background:#eee}}
.pill.online{{color:#067647;background:#ecfdf3}} .pill.offline{{color:#b42318;background:#fef3f2}}
.badge{{border-radius:999px;padding:2px 7px;font-size:12px;background:#eef2f6;color:#364152}}
.badge.ok{{background:#ecfdf3;color:#067647}} .badge.warning{{background:#fffaeb;color:#b54708}} .badge.error{{background:#fef3f2;color:#b42318}}
.error{{color:#b42318;margin:8px 0}}
.small{{border:1px solid #d0d5dd;border-radius:5px;background:#fff;padding:3px 7px;font-size:12px;cursor:pointer}}
.danger{{color:#b42318;border-color:#fda29b}} .danger-strong{{color:#fff;background:#b42318;border-color:#b42318}}
.panel{{background:#fff;border:1px solid #d8d8d0;border-radius:8px;padding:14px;margin:14px 0;overflow:hidden}}
.health{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}}
.health>div{{background:#fff;border:1px solid #d8d8d0;border-radius:8px;padding:12px}}
.health strong{{display:block;font-size:18px}} .health span{{color:#667085;font-size:12px}}
.gpu-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px;margin-top:10px}}
.gpu-card{{border:1px solid #ecece6;border-radius:8px;padding:12px;background:#fcfcfa;overflow:hidden}}
.gpu-card h3 span{{color:#667085;font-weight:500}}
table{{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}}
th,td{{border-top:1px solid #eee;padding:7px;text-align:left;vertical-align:top}} th{{color:#667085}}
@media(max-width:640px){{main{{width:calc(100vw - 20px)}}.grid{{grid-template-columns:1fr}}.toolbar{{align-items:flex-start;flex-direction:column}}}}
</style></head><body><main>{body}</main>
<script>
let paused = false;
const actionToken = "{esc(ServerHandler.action_token)}";
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
