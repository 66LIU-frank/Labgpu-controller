from __future__ import annotations

import html
import json
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from labgpu.remote.cache import read_server_cache, write_server_cache
from labgpu.remote.probe import probe_host
from labgpu.remote.ssh_config import SSHHost, parse_ssh_config, resolve_ssh_host, select_hosts


def serve(
    *,
    host: str,
    port: int,
    ssh_config: str | Path | None = None,
    names: list[str] | None = None,
    pattern: str | None = None,
    timeout: int = 8,
    open_browser: bool = False,
) -> None:
    ServerHandler.ssh_config = ssh_config
    ServerHandler.names = names
    ServerHandler.pattern = pattern
    ServerHandler.timeout = timeout
    if host == "0.0.0.0":
        print("Warning: LabGPU servers dashboard has no authentication in this version.")
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
    hosts = select_hosts(parse_ssh_config(ssh_config), names=names, pattern=pattern)
    if not hosts:
        return {"hosts": [], "count": 0, "error": "no SSH hosts selected"}
    hosts = [resolve_ssh_host(host) for host in hosts]
    results = []
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as executor:
        futures = {executor.submit(probe_host, host, timeout=timeout): host for host in hosts}
        for future in as_completed(futures):
            result = future.result()
            if result.get("online"):
                write_server_cache(result)
            else:
                cached = read_server_cache(str(result.get("alias") or futures[future].alias))
                if cached:
                    result["cached"] = cached
                    result["last_seen"] = cached.get("probed_at")
            results.append(result)
    results.sort(key=lambda item: str(item.get("alias")))
    return {"hosts": results, "count": len(results), "error": None}


class ServerHandler(BaseHTTPRequestHandler):
    ssh_config: str | Path | None = None
    names: list[str] | None = None
    pattern: str | None = None
    timeout: int = 8

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

    def log_message(self, format: str, *args: object) -> None:
        return

    def _data(self, query: str) -> dict[str, object]:
        params = parse_qs(query)
        names = self.names
        if params.get("hosts"):
            names = split_hosts(params["hosts"][0])
        pattern = params.get("pattern", [self.pattern])[0]
        return collect_servers(
            ssh_config=self.ssh_config,
            names=names,
            pattern=pattern,
            timeout=self.timeout,
        )

    def _data_for_alias(self, alias: str) -> dict[str, object]:
        return collect_servers(
            ssh_config=self.ssh_config,
            names=[alias],
            pattern=None,
            timeout=self.timeout,
        )

    def _html(self, body: str) -> None:
        self._send("text/html; charset=utf-8", body.encode("utf-8"))

    def _json(self, value: object) -> None:
        self._send("application/json; charset=utf-8", json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8"))

    def _send(self, content_type: str, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def render_index(data: dict[str, object]) -> str:
    hosts = data.get("hosts") or []
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
        <section class="grid">{cards}</section>
        """,
    )


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
        <section class="panel"><h2>GPUs</h2><div class="gpu-grid">{''.join(render_gpu_card(gpu) for gpu in host.get('gpus') or [])}</div></section>
        <section class="panel"><h2>Processes</h2>{render_process_table(host.get('processes') or [])}</section>
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


def render_gpu_card(gpu: object) -> str:
    if not isinstance(gpu, dict):
        return ""
    processes = gpu.get("processes") or []
    rows = "".join(render_process_row(proc, include_gpu=False) for proc in processes if isinstance(proc, dict))
    if not rows:
        rows = "<tr><td colspan='4' class='muted'>free</td></tr>"
    return f"""
    <article class="gpu-card">
      <h3>GPU {esc(gpu.get('index'))} <span>{esc(short(gpu.get('name') or '', 32))}</span></h3>
      <div class="meta">
        <span>{esc(gpu.get('memory_used_mb'))}/{esc(gpu.get('memory_total_mb'))} MB</span>
        <span>{esc(gpu.get('utilization_gpu'))}% util</span>
        <span>{esc(gpu.get('temperature'))} C</span>
      </div>
      <table><tr><th>User</th><th>PID</th><th>Memory</th><th>Command</th></tr>{rows}</table>
    </article>
    """


def render_process_table(processes: object) -> str:
    if not isinstance(processes, list) or not processes:
        return "<p class='muted'>No GPU compute processes.</p>"
    rows = "".join(render_process_row(proc, include_gpu=True) for proc in processes if isinstance(proc, dict))
    return f"<table><tr><th>GPU UUID</th><th>User</th><th>PID</th><th>Memory</th><th>Command</th><th>Hint</th></tr>{rows}</table>"


def render_process_row(proc: dict[str, object], *, include_gpu: bool) -> str:
    command = short(proc.get("command") or "", 140)
    hint = f"labgpu adopt {esc(proc.get('pid'))} --name NAME"
    gpu = f"<td>{esc(short(proc.get('gpu_uuid') or '', 14))}</td>" if include_gpu else ""
    hint_cell = f"<td><code>{hint}</code></td>" if include_gpu else ""
    return (
        f"<tr>{gpu}<td>{esc(proc.get('user') or '?')}</td><td>{esc(proc.get('pid'))}</td>"
        f"<td>{esc(proc.get('used_memory_mb'))} MB</td><td><code>{esc(command)}</code></td>{hint_cell}</tr>"
    )


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
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}}
.card{{background:#fff;border:1px solid #d8d8d0;border-radius:8px;padding:14px;overflow:hidden}}
.card-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px}}
.meta{{display:flex;flex-wrap:wrap;gap:10px;margin:6px 0;color:#667085;font-size:13px}}
.pill{{border-radius:999px;padding:2px 9px;font-size:12px;background:#eee}}
.pill.online{{color:#067647;background:#ecfdf3}} .pill.offline{{color:#b42318;background:#fef3f2}}
.error{{color:#b42318;margin:8px 0}}
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
