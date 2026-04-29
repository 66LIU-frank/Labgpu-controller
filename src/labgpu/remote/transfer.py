from __future__ import annotations

import json
import re
import select
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any

MIB = 1024 * 1024
CHUNK_SIZE = MIB
DEFAULT_EXCLUDES = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".DS_Store",
    ".venv",
    "venv",
    "node_modules",
    "wandb",
    "outputs",
    "checkpoints",
)
ALIAS_RE = re.compile(r"^[A-Za-z0-9_.@+-]+$")


@dataclass(frozen=True)
class RemotePath:
    host: str
    path: str


@dataclass(frozen=True)
class TransferPlan:
    source: RemotePath
    target: RemotePath
    excludes: tuple[str, ...]
    source_command: str
    target_command: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": f"{self.source.host}:{self.source.path}",
            "target": f"{self.target.host}:{self.target.path}",
            "excludes": list(self.excludes),
            "source_command": self.source_command,
            "target_command": self.target_command,
            "copyable_pipeline": copyable_pipeline(self),
        }


@dataclass(frozen=True)
class SpeedResult:
    direction: str
    ok: bool
    bytes: int
    seconds: float
    mb_per_second: float
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "ok": self.ok,
            "bytes": self.bytes,
            "seconds": round(self.seconds, 3),
            "mb_per_second": round(self.mb_per_second, 2),
            "message": self.message,
        }


def parse_remote_path(spec: str) -> RemotePath:
    if ":" not in spec:
        raise ValueError("remote path must look like HOST:/path/to/project")
    host, path = spec.split(":", 1)
    host = host.strip()
    path = path.strip()
    validate_ssh_alias(host)
    if not path:
        raise ValueError("remote path cannot be empty")
    return RemotePath(host=host, path=path)


def validate_ssh_alias(alias: str) -> None:
    if not alias or alias.startswith("-") or not ALIAS_RE.match(alias):
        raise ValueError(f"unsafe SSH alias: {alias!r}")


def build_transfer_plan(
    source_spec: str,
    target_spec: str,
    *,
    excludes: list[str] | None = None,
    no_default_excludes: bool = False,
) -> TransferPlan:
    source = parse_remote_path(source_spec)
    target = parse_remote_path(target_spec)
    exclude_values = tuple(excludes or ()) if no_default_excludes else tuple(dict.fromkeys([*DEFAULT_EXCLUDES, *(excludes or [])]))
    source_command = build_source_tar_command(source.path, exclude_values)
    target_command = build_target_tar_command(target.path)
    return TransferPlan(source=source, target=target, excludes=exclude_values, source_command=source_command, target_command=target_command)


def build_source_tar_command(path: str, excludes: tuple[str, ...]) -> str:
    exclude_flags = " ".join(shlex.quote(f"--exclude={pattern}") for pattern in excludes)
    tar_part = f"tar {exclude_flags} -cf - ." if exclude_flags else "tar -cf - ."
    return f"cd {shlex.quote(path)} && {tar_part}"


def build_target_tar_command(path: str) -> str:
    quoted = shlex.quote(path)
    return f"mkdir -p {quoted} && tar -xf - -C {quoted}"


def copyable_pipeline(plan: TransferPlan) -> str:
    left = shlex.join(["ssh", plan.source.host, plan.source_command])
    right = shlex.join(["ssh", plan.target.host, plan.target_command])
    return f"{left} | {right}"


def run_transfer_plan(plan: TransferPlan, *, timeout: int = 3600) -> dict[str, Any]:
    started = time.monotonic()
    source_proc = subprocess.Popen(
        ["ssh", plan.source.host, plan.source_command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    target_proc = subprocess.Popen(
        ["ssh", plan.target.host, plan.target_command],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    copied = 0
    error = ""
    try:
        if source_proc.stdout is None or target_proc.stdin is None:
            raise RuntimeError("failed to open transfer pipes")
        while True:
            if time.monotonic() - started > timeout:
                raise TimeoutError(f"transfer timed out after {timeout}s")
            ready, _, _ = select.select([source_proc.stdout], [], [], 0.5)
            if not ready:
                if source_proc.poll() is not None:
                    break
                continue
            chunk = source_proc.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            copied += len(chunk)
            target_proc.stdin.write(chunk)
        target_proc.stdin.close()
        source_stderr = source_proc.stderr.read().decode("utf-8", errors="replace") if source_proc.stderr else ""
        target_stderr = target_proc.stderr.read().decode("utf-8", errors="replace") if target_proc.stderr else ""
        source_code = source_proc.wait(timeout=5)
        target_code = target_proc.wait(timeout=5)
        ok = source_code == 0 and target_code == 0
        error = "\n".join(part for part in (source_stderr.strip(), target_stderr.strip()) if part)
    except Exception as exc:  # noqa: BLE001 - return concise transfer failures to CLI.
        ok = False
        error = str(exc)
        _kill_process(source_proc)
        _kill_process(target_proc)
    seconds = max(0.001, time.monotonic() - started)
    return {
        "ok": ok,
        "source": f"{plan.source.host}:{plan.source.path}",
        "target": f"{plan.target.host}:{plan.target.path}",
        "bytes": copied,
        "seconds": round(seconds, 3),
        "mb_per_second": round((copied / MIB) / seconds, 2),
        "message": error,
    }


def planned_nettests(source: str, target: str, *, mb: int, both: bool = False, direct: bool = False) -> list[str]:
    validate_ssh_alias(source)
    validate_ssh_alias(target)
    tests = [
        f"local -> {source} upload",
        f"local -> {target} upload",
        f"{source} -> {target} via local relay",
    ]
    if both:
        tests.append(f"{target} -> {source} via local relay")
    if direct:
        tests.append(f"{source} -> {target} direct ssh")
        if both:
            tests.append(f"{target} -> {source} direct ssh")
    return [f"{name} ({mb} MiB)" for name in tests]


def run_nettests(source: str, target: str, *, mb: int = 32, timeout: int = 60, both: bool = False, direct: bool = False) -> list[SpeedResult]:
    validate_ssh_alias(source)
    validate_ssh_alias(target)
    if mb <= 0:
        raise ValueError("--mb must be positive")
    results = [
        measure_upload(source, mb=mb, timeout=timeout),
        measure_upload(target, mb=mb, timeout=timeout),
        measure_relay(source, target, mb=mb, timeout=timeout),
    ]
    if both:
        results.append(measure_relay(target, source, mb=mb, timeout=timeout))
    if direct:
        results.append(measure_direct(source, target, mb=mb, timeout=timeout))
        if both:
            results.append(measure_direct(target, source, mb=mb, timeout=timeout))
    return results


def measure_upload(host: str, *, mb: int, timeout: int) -> SpeedResult:
    payload = b"\0" * (mb * MIB)
    start = time.monotonic()
    proc = subprocess.Popen(["ssh", host, "cat > /dev/null"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _, stderr = proc.communicate(input=payload, timeout=timeout)
        ok = proc.returncode == 0
        message = stderr.decode("utf-8", errors="replace").strip()
    except subprocess.TimeoutExpired:
        _kill_process(proc)
        ok = False
        message = f"timed out after {timeout}s"
    seconds = max(0.001, time.monotonic() - start)
    return _speed_result(f"local -> {host} upload", ok, len(payload) if ok else 0, seconds, message)


def measure_relay(source: str, target: str, *, mb: int, timeout: int) -> SpeedResult:
    start = time.monotonic()
    copied = 0
    source_proc = subprocess.Popen(_dd_ssh_command(source, mb), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    target_proc = subprocess.Popen(["ssh", target, "cat > /dev/null"], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    message = ""
    ok = False
    try:
        if source_proc.stdout is None or target_proc.stdin is None:
            raise RuntimeError("failed to open nettest pipes")
        while True:
            if time.monotonic() - start > timeout:
                raise TimeoutError(f"timed out after {timeout}s")
            ready, _, _ = select.select([source_proc.stdout], [], [], 0.5)
            if not ready:
                if source_proc.poll() is not None:
                    break
                continue
            chunk = source_proc.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            copied += len(chunk)
            target_proc.stdin.write(chunk)
        target_proc.stdin.close()
        source_stderr = source_proc.stderr.read().decode("utf-8", errors="replace") if source_proc.stderr else ""
        target_stderr = target_proc.stderr.read().decode("utf-8", errors="replace") if target_proc.stderr else ""
        source_code = source_proc.wait(timeout=5)
        target_code = target_proc.wait(timeout=5)
        ok = source_code == 0 and target_code == 0
        message = "\n".join(part for part in (source_stderr.strip(), target_stderr.strip()) if part)
    except Exception as exc:  # noqa: BLE001 - return concise nettest failures to CLI.
        message = str(exc)
        _kill_process(source_proc)
        _kill_process(target_proc)
    seconds = max(0.001, time.monotonic() - start)
    return _speed_result(f"{source} -> {target} via local relay", ok, copied if ok else 0, seconds, message)


def measure_direct(source: str, target: str, *, mb: int, timeout: int) -> SpeedResult:
    validate_ssh_alias(source)
    validate_ssh_alias(target)
    remote = f"dd if=/dev/zero bs={MIB} count={int(mb)} 2>/dev/null | ssh -o BatchMode=yes {shlex.quote(target)} 'cat > /dev/null'"
    start = time.monotonic()
    proc = subprocess.Popen(["ssh", source, remote], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _, stderr = proc.communicate(timeout=timeout)
        ok = proc.returncode == 0
        message = stderr.decode("utf-8", errors="replace").strip()
    except subprocess.TimeoutExpired:
        _kill_process(proc)
        ok = False
        message = f"timed out after {timeout}s"
    seconds = max(0.001, time.monotonic() - start)
    return _speed_result(f"{source} -> {target} direct ssh", ok, mb * MIB if ok else 0, seconds, message)


def _dd_ssh_command(host: str, mb: int) -> list[str]:
    return ["ssh", host, f"dd if=/dev/zero bs={MIB} count={int(mb)} 2>/dev/null"]


def _speed_result(direction: str, ok: bool, byte_count: int, seconds: float, message: str = "") -> SpeedResult:
    mb_per_second = (byte_count / MIB) / seconds if ok and byte_count else 0.0
    return SpeedResult(direction=direction, ok=ok, bytes=byte_count, seconds=seconds, mb_per_second=mb_per_second, message=message)


def _kill_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def dumps_json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
