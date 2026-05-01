from __future__ import annotations

import hashlib
import http.client
import json
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SESSION_TOKEN_PREFIX = "labgpu-session-"
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60
DEFAULT_MAX_LIFETIME_SECONDS = 2 * 60 * 60
DEFAULT_CLEANUP_INTERVAL_SECONDS = 30
STREAM_CHUNK_SIZE = 64 * 1024
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass
class GatewayState:
    token: str
    created_at: float
    last_accessed: float
    idle_timeout_seconds: float
    max_lifetime_seconds: float
    metadata: dict[str, Any] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.Lock, repr=False)

    def touch(self, now: float | None = None) -> None:
        with self.lock:
            self.last_accessed = time.monotonic() if now is None else now

    def is_expired(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self.lock:
            idle_expired = current - self.last_accessed >= self.idle_timeout_seconds
            lifetime_expired = current - self.created_at >= self.max_lifetime_seconds
        return idle_expired or lifetime_expired


@dataclass
class AIGatewaySession:
    state: GatewayState
    target_host: str
    target_port: int
    listen_host: str
    listen_port: int
    server: ThreadingHTTPServer
    thread: threading.Thread
    stop_event: threading.Event
    cleanup_thread: threading.Thread | None = None
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def token(self) -> str:
        return self.state.token

    @property
    def token_fingerprint(self) -> str:
        return token_fingerprint(self.token)

    def touch(self) -> None:
        self.state.touch()

    def is_expired(self, now: float | None = None) -> bool:
        return self.state.is_expired(now)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.stop_event.set()
        self.server.shutdown()
        self.server.server_close()
        current = threading.current_thread()
        if self.thread is not current:
            self.thread.join(timeout=2)
        if self.cleanup_thread and self.cleanup_thread is not current:
            self.cleanup_thread.join(timeout=2)


def new_session_token() -> str:
    return f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def token_fingerprint(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:12]


def start_ai_gateway(
    *,
    target_port: int,
    token: str | None = None,
    target_host: str = "127.0.0.1",
    listen_host: str = "127.0.0.1",
    listen_port: int = 0,
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    max_lifetime_seconds: float = DEFAULT_MAX_LIFETIME_SECONDS,
    cleanup_interval_seconds: float = DEFAULT_CLEANUP_INTERVAL_SECONDS,
    metadata: dict[str, Any] | None = None,
) -> AIGatewaySession:
    if listen_host != "127.0.0.1":
        raise ValueError("AI gateway must listen on 127.0.0.1.")
    if target_host != "127.0.0.1":
        raise ValueError("AI gateway target must be 127.0.0.1.")
    validate_port(target_port, "Target proxy port")
    validate_port(listen_port, "Gateway listen port", allow_zero=True)
    session_token = token or new_session_token()
    if not is_session_token(session_token):
        raise ValueError("AI gateway token must be a LabGPU session token.")
    now = time.monotonic()
    state = GatewayState(
        token=session_token,
        created_at=now,
        last_accessed=now,
        idle_timeout_seconds=idle_timeout_seconds,
        max_lifetime_seconds=max_lifetime_seconds,
        metadata=safe_session_metadata(metadata or {}),
    )
    handler = build_gateway_handler(state=state, target_host=target_host, target_port=target_port)
    server = ThreadingHTTPServer((listen_host, listen_port), handler)
    actual_host, actual_port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, name=f"labgpu-ai-gateway-{actual_port}", daemon=True)
    stop_event = threading.Event()
    session = AIGatewaySession(
        state=state,
        target_host=target_host,
        target_port=target_port,
        listen_host=str(actual_host),
        listen_port=int(actual_port),
        server=server,
        thread=thread,
        stop_event=stop_event,
    )
    cleanup_thread = threading.Thread(
        target=gateway_cleanup_worker,
        args=(session, cleanup_interval_seconds),
        name=f"labgpu-ai-gateway-cleanup-{actual_port}",
        daemon=True,
    )
    session.cleanup_thread = cleanup_thread
    thread.start()
    cleanup_thread.start()
    return session


def gateway_cleanup_worker(session: AIGatewaySession, check_interval_seconds: float) -> None:
    while not session.stop_event.wait(check_interval_seconds):
        if session.is_expired():
            session.close()
            return


def build_gateway_handler(*, state: GatewayState, target_host: str, target_port: int) -> type[BaseHTTPRequestHandler]:
    class AIGatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_POST(self) -> None:  # noqa: N802
            self._handle()

        def do_PUT(self) -> None:  # noqa: N802
            self._handle()

        def do_PATCH(self) -> None:  # noqa: N802
            self._handle()

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._handle()

        def _handle(self) -> None:
            if state.is_expired():
                self._send_plain(401, "Unauthorized\n")
                return
            if not request_has_token(self.headers, state.token):
                self._send_plain(401, "Unauthorized\n")
                return
            state.touch()
            if self.path.split("?", 1)[0] == "/__labgpu/session":
                if self.command != "GET":
                    self._send_plain(405, "Method Not Allowed\n")
                    return
                self._send_json(200, session_health_payload(state, target_host=target_host, target_port=target_port))
                return
            body = read_request_body(self)
            conn: http.client.HTTPConnection | None = None
            try:
                conn, response = open_upstream_response(
                    method=self.command,
                    path=self.path,
                    headers=self.headers,
                    body=body,
                    target_host=target_host,
                    target_port=target_port,
                )
            except OSError:
                self._send_plain(502, "Bad Gateway\n")
                return
            try:
                response_headers = response.getheaders()
                streaming = is_streaming_response(response_headers)
                self.send_response(response.status, response.reason)
                for key, value in filtered_response_headers(response_headers, include_content_length=False):
                    self.send_header(key, value)
                if streaming:
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    stream_response(response, self.wfile)
                else:
                    response_body = response.read()
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
            finally:
                if conn:
                    conn.close()

        def _send_plain(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: int, value: dict[str, Any]) -> None:
            payload = json.dumps(value, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return AIGatewayHandler


def safe_session_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key in ("mode", "app", "provider", "server", "remote_cwd", "ccswitch_proxy_port"):
        value = str(metadata.get(key) or "").strip()
        if value:
            safe[key] = value[:512]
    return safe


def session_health_payload(state: GatewayState, *, target_host: str, target_port: int) -> dict[str, Any]:
    now = time.monotonic()
    with state.lock:
        idle_remaining = max(0, int(state.idle_timeout_seconds - (now - state.last_accessed)))
        lifetime_remaining = max(0, int(state.max_lifetime_seconds - (now - state.created_at)))
        metadata = dict(state.metadata)
    return {
        "ok": True,
        "target_host": target_host,
        "target_port": target_port,
        "token_fingerprint": token_fingerprint(state.token),
        "idle_timeout_seconds": int(state.idle_timeout_seconds),
        "max_lifetime_seconds": int(state.max_lifetime_seconds),
        "idle_timeout_remaining_seconds": idle_remaining,
        "max_lifetime_remaining_seconds": lifetime_remaining,
        **metadata,
    }


def request_has_token(headers: Any, token: str) -> bool:
    api_key = str(header_value(headers, "x-api-key") or "").strip()
    if secrets.compare_digest(api_key, token):
        return True
    authorization = str(header_value(headers, "authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        authorization = authorization[7:].strip()
    return secrets.compare_digest(authorization, token)


def header_value(headers: Any, name: str) -> Any:
    value = headers.get(name)
    if value is not None:
        return value
    return headers.get(name.title())


def read_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError:
        length = 0
    return handler.rfile.read(length) if length > 0 else b""


def forward_request(
    *,
    method: str,
    path: str,
    headers: Any,
    body: bytes,
    target_host: str,
    target_port: int,
) -> tuple[int, str, list[tuple[str, str]], bytes]:
    conn, response = open_upstream_response(
        method=method,
        path=path,
        headers=headers,
        body=body,
        target_host=target_host,
        target_port=target_port,
    )
    try:
        response_body = response.read()
        return response.status, response.reason, response.getheaders(), response_body
    finally:
        conn.close()


def open_upstream_response(
    *,
    method: str,
    path: str,
    headers: Any,
    body: bytes,
    target_host: str,
    target_port: int,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    outbound_headers = rewrite_headers(headers, target_host=target_host, target_port=target_port)
    conn = http.client.HTTPConnection(target_host, target_port, timeout=30)
    try:
        conn.request(method, path, body=body, headers=outbound_headers)
        return conn, conn.getresponse()
    except Exception:
        conn.close()
        raise


def rewrite_headers(headers: Any, *, target_host: str, target_port: int) -> dict[str, str]:
    rewritten: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in {"host", "authorization", "x-api-key"}:
            continue
        rewritten[key] = value
    rewritten["Host"] = f"{target_host}:{target_port}"
    return rewritten


def is_streaming_response(headers: list[tuple[str, str]]) -> bool:
    values = {key.lower(): value.lower() for key, value in headers}
    content_type = values.get("content-type", "")
    transfer_encoding = values.get("transfer-encoding", "")
    return "text/event-stream" in content_type or "chunked" in transfer_encoding


def filtered_response_headers(headers: list[tuple[str, str]], *, include_content_length: bool) -> list[tuple[str, str]]:
    filtered = []
    for key, value in headers:
        lower = key.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        if lower == "content-length" and not include_content_length:
            continue
        filtered.append((key, value))
    return filtered


def stream_response(response: Any, writer: Any, *, chunk_size: int = STREAM_CHUNK_SIZE) -> None:
    read_chunk = getattr(response, "read1", None) or response.read
    while True:
        chunk = read_chunk(chunk_size)
        if not chunk:
            return
        writer.write(chunk)
        flush = getattr(writer, "flush", None)
        if flush:
            flush()


def is_session_token(value: str) -> bool:
    token = str(value or "")
    if token.startswith("sk-") or token.startswith("AKIA"):
        return False
    suffix = token.removeprefix(SESSION_TOKEN_PREFIX)
    return token.startswith(SESSION_TOKEN_PREFIX) and len(suffix) >= 24 and all(ch.isalnum() or ch in "-_" for ch in suffix)


def validate_port(value: int, label: str, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if value < minimum or value > 65535:
        raise ValueError(f"{label} must be between {minimum} and 65535.")


def is_local_tcp_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False
