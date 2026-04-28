from __future__ import annotations

import html
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from labgpu.remote.probe import probe_host
from labgpu.remote.ssh_config import SSHHost, parse_ssh_config, select_hosts


def serve(
    *,
    host: str,
    port: int,
    ssh_config: str | Path | None = None,
    names: list[str] | None = None,
    pattern: str | None = None,
    timeout: int = 8,
) -> None:
    ServerHandler.ssh_config = ssh_config
    ServerHandler.names = names
    ServerHandler.pattern = pattern
    ServerHandler.timeout = timeout
    if host == "0.0.0.0":
        print("Warning: LabGPU servers dashboard has no authentication in this version.")
    server = ThreadingHTTPServer((host, port), ServerHandler)
    print(f"LabGPU servers dashboard: http://{host}:{port}")
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
    results = []
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as executor:
        futures = {executor.submit(probe_host, host, timeout=timeout): host for host in hosts}
        for future in as_completed(futures):
            results.append(future.result())
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
        "LabGPU Servers",
        f"""
        <section class="toolbar">
          <div>
            <h1>LabGPU Servers</h1>
            <p>Local SSH dashboard for lab GPU machines</p>
          </div>
          <a class="button" href="/api/servers">JSON</a>
        </section>
        <section class="grid">{cards}</section>
        """,
    )


def render_host_card(host: object) -> str:
    if not isinstance(host, dict):
        return ""
    online = bool(host.get("online"))
    status = "online" if online else "offline"
    error = host.get("error")
    gpus = host.get("gpus") or []
    disk = host.get("disk") or {}
    gpu_rows = "".join(render_gpu_row(gpu) for gpu in gpus) or "<tr><td colspan='5' class='muted'>No GPU data.</td></tr>"
    return f"""
    <article class="card">
      <div class="card-head">
        <div>
          <h2>{esc(host.get('alias'))}</h2>
          <p>{esc(host.get('remote_hostname') or host.get('hostname') or '')}</p>
        </div>
        <span class="pill {status}">{status}</span>
      </div>
      <div class="meta">
        <span>user {esc(host.get('user') or '-')}</span>
        <span>port {esc(host.get('port') or '22')}</span>
        <span>{esc(host.get('elapsed_ms'))} ms</span>
      </div>
      <div class="meta">
        <span>{esc(host.get('uptime') or '-')}</span>
        <span>load {esc(host.get('load') or '-')}</span>
        <span>disk {esc(disk.get('use_percent') if isinstance(disk, dict) else '-')}</span>
      </div>
      {f"<p class='error'>{esc(error)}</p>" if error else ""}
      <table>
        <tr><th>GPU</th><th>Name</th><th>Memory</th><th>Util</th><th>Processes</th></tr>
        {gpu_rows}
      </table>
    </article>
    """


def render_gpu_row(gpu: object) -> str:
    if not isinstance(gpu, dict):
        return ""
    processes = gpu.get("processes") or []
    process_text = "<br>".join(
        f"{esc(proc.get('user') or '?')} pid {esc(proc.get('pid'))} {esc(proc.get('used_memory_mb'))}MB {esc(short(proc.get('command') or '', 60))}"
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


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>{esc(title)}</title>
<style>
body{{font:14px/1.45 system-ui,sans-serif;margin:0;background:#f7f7f4;color:#1f2328}}
main{{width:min(1280px,calc(100vw - 32px));margin:0 auto;padding:22px 0 36px}}
h1,h2,p{{margin:0}} h1{{font-size:28px}} h2{{font-size:18px}} p,.muted{{color:#667085}}
.toolbar{{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:18px}}
.button{{border:1px solid #d0d5dd;background:#fff;border-radius:6px;padding:7px 10px;color:#1f2328;text-decoration:none}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}}
.card{{background:#fff;border:1px solid #d8d8d0;border-radius:8px;padding:14px;overflow:hidden}}
.card-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px}}
.meta{{display:flex;flex-wrap:wrap;gap:10px;margin:6px 0;color:#667085;font-size:13px}}
.pill{{border-radius:999px;padding:2px 9px;font-size:12px;background:#eee}}
.pill.online{{color:#067647;background:#ecfdf3}} .pill.offline{{color:#b42318;background:#fef3f2}}
.error{{color:#b42318;margin:8px 0}}
table{{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}}
th,td{{border-top:1px solid #eee;padding:7px;text-align:left;vertical-align:top}} th{{color:#667085}}
@media(max-width:640px){{main{{width:calc(100vw - 20px)}}.grid{{grid-template-columns:1fr}}.toolbar{{align-items:flex-start;flex-direction:column}}}}
</style></head><body><main>{body}</main></body></html>"""


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
