from __future__ import annotations

import html
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from labgpu.cli.status import collect_status
from labgpu.core.refresh import refresh_runs
from labgpu.core.store import RunStore


def serve(host: str, port: int, *, fake: bool = False) -> None:
    if host == "0.0.0.0":
        print("Warning: LabGPU web has no authentication in this version.")
    Handler.fake = fake
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"LabGPU web: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


class Handler(BaseHTTPRequestHandler):
    fake = False

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._html(render_index(self.fake))
        elif path == "/api/status":
            self._json(collect_status(fake=self.fake))
        elif path == "/api/runs":
            store = RunStore()
            refresh_runs(store)
            self._json([item.to_dict() for item in store.list(all_runs=True)])
        elif path.startswith("/api/runs/") and path.endswith("/logs"):
            ref = unquote(path.removeprefix("/api/runs/").removesuffix("/logs").strip("/"))
            self._text(run_log(ref))
        elif path.startswith("/api/runs/") and path.endswith("/diagnosis"):
            ref = unquote(path.removeprefix("/api/runs/").removesuffix("/diagnosis").strip("/"))
            self._json(run_diagnosis(ref))
        elif path.startswith("/api/runs/"):
            ref = unquote(path.removeprefix("/api/runs/").strip("/"))
            self._json(run_json(ref))
        elif path.startswith("/runs/"):
            self._html(render_run(unquote(path.removeprefix("/runs/"))))
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _html(self, body: str) -> None:
        self._send("text/html; charset=utf-8", body.encode("utf-8"))

    def _json(self, value: object) -> None:
        self._send("application/json; charset=utf-8", json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8"))

    def _text(self, body: str) -> None:
        self._send("text/plain; charset=utf-8", body.encode("utf-8", errors="replace"))

    def _send(self, content_type: str, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def render_index(fake: bool) -> str:
    status = collect_status(fake=fake)
    store = RunStore()
    refresh_runs(store)
    runs = store.list(all_runs=True)[:50]
    running = [meta for meta in runs if meta.status == "running"]
    failures = [meta for meta in runs if meta.status == "failed"]
    gpu_rows = []
    for gpu in status["gpu"].get("gpus", []):
        processes = gpu.get("processes") or [{}]
        for proc in processes:
            exp = proc.get("experiment") or {}
            gpu_rows.append(
                f"<tr><td>{esc(gpu.get('index'))}</td><td>{esc(gpu.get('name'))}</td>"
                f"<td>{esc(gpu.get('memory_used_mb'))}/{esc(gpu.get('memory_total_mb'))} MB</td>"
                f"<td>{esc(gpu.get('utilization_gpu'))}%</td><td>{esc(proc.get('user') or '-')}</td>"
                f"<td>{esc(proc.get('pid') or '-')}</td><td>{esc(exp.get('name') or gpu.get('labgpu_state'))}</td>"
                f"<td>{esc(adopt_hint(proc, exp))}</td></tr>"
            )
    run_rows = [run_row(meta) for meta in runs]
    running_rows = [run_row(meta) for meta in running] or ["<tr><td colspan='5'>No running experiments.</td></tr>"]
    failure_rows = [run_row(meta) for meta in failures] or ["<tr><td colspan='5'>No recent failures.</td></tr>"]
    return page(
        "LabGPU",
        f"""
        <h1>LabGPU Dashboard</h1>
        <h2>GPU Overview</h2>
        <table><tr><th>GPU</th><th>Name</th><th>Memory</th><th>Util</th><th>User</th><th>PID</th><th>Experiment</th><th>Action</th></tr>{''.join(gpu_rows)}</table>
        <h2>Running Experiments</h2>
        <table><tr><th>Name</th><th>Status</th><th>User</th><th>GPU</th><th>Reason</th></tr>{''.join(running_rows)}</table>
        <h2>Recent Failures</h2>
        <table><tr><th>Name</th><th>Status</th><th>User</th><th>GPU</th><th>Reason</th></tr>{''.join(failure_rows)}</table>
        <h2>Recent Experiments</h2>
        <table><tr><th>Name</th><th>Status</th><th>User</th><th>GPU</th><th>Reason</th></tr>{''.join(run_rows)}</table>
        """,
    )


def run_row(meta) -> str:
    return (
        f"<tr><td><a href='/runs/{esc(meta.run_id)}'>{esc(meta.name)}</a></td><td>{esc(meta.status)}</td>"
        f"<td>{esc(meta.user)}</td><td>{esc(meta.cuda_visible_devices or '-')}</td><td>{esc(meta.failure_reason or '-')}</td></tr>"
    )


def adopt_hint(proc: dict[str, object], exp: dict[str, object]) -> str:
    if exp:
        return ""
    pid = proc.get("pid")
    user = proc.get("user")
    if not pid or not user:
        return ""
    return f"labgpu adopt {pid} --name NAME"


def render_run(ref: str) -> str:
    store = RunStore()
    meta = store.resolve(ref)
    if not meta:
        return page("Not found", "<p>Run not found.</p>")
    rows = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in meta.to_dict().items() if v not in (None, "", []))
    return page(
        meta.name,
        f"<h1>{esc(meta.name)}</h1><table>{rows}</table><h2>Diagnosis</h2><pre>{esc(json.dumps(run_diagnosis(meta.run_id), indent=2, ensure_ascii=False))}</pre><h2>Log Tail</h2><pre>{esc(run_log(meta.run_id))}</pre>",
    )


def run_json(ref: str) -> dict[str, object]:
    store = RunStore()
    meta = store.resolve(ref)
    if not meta:
        return {"error": "run not found"}
    return meta.to_dict()


def run_diagnosis(ref: str) -> dict[str, object]:
    store = RunStore()
    meta = store.resolve(ref)
    if not meta:
        return {"error": "run not found"}
    path = store.run_dir(meta.run_id) / "diagnosis.json"
    if not path.exists():
        return {"type": "unknown", "title": "No diagnosis yet", "severity": "info", "evidence": None, "line_number": None, "suggestion": "Run labgpu diagnose or wait for the run to finish."}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"type": "unknown", "title": "Corrupt diagnosis file", "severity": "warning", "evidence": None, "line_number": None, "suggestion": "Regenerate diagnosis with labgpu diagnose."}


def run_log(ref: str) -> str:
    store = RunStore()
    meta = store.resolve(ref)
    if not meta or not meta.log_path:
        return "log not found\n"
    path = Path(meta.log_path)
    if not path.exists():
        return "log not found\n"
    data = path.read_bytes()
    return b"\n".join(data.splitlines()[-120:]).decode(errors="replace")


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><style>
body{{font:14px/1.45 system-ui,sans-serif;margin:24px;background:#f7f7f4;color:#1f2328}}
main{{max-width:1120px;margin:auto}} h1{{font-size:28px}} h2{{font-size:16px;margin-top:24px}}
table{{width:100%;border-collapse:collapse;background:white;border:1px solid #ddd;border-radius:8px;overflow:hidden}}
th,td{{border-bottom:1px solid #eee;padding:8px;text-align:left;vertical-align:top}} th{{color:#666}}
pre{{white-space:pre-wrap;background:#111;color:#f5f5f5;padding:12px;border-radius:8px;overflow:auto}}
a{{color:#0f766e;text-decoration:none}}
</style></head><body><main>{body}</main></body></html>"""


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)
