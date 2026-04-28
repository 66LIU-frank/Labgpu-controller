from __future__ import annotations

import csv
import hashlib
import json
import re
import shlex
import subprocess
import time
from io import StringIO
from typing import Any

from labgpu.remote.ssh_config import SSHHost
from labgpu.utils.time import now_utc


DEFAULT_DISK_PATHS = ["/", "/home", "/data", "/scratch", "/mnt", "/nvme"]


REMOTE_SCRIPT_TEMPLATE = r"""
set +e
echo "__LABGPU_SECTION__ host"
hostname 2>/dev/null
echo "__LABGPU_SECTION__ current_user"
(whoami 2>/dev/null || id -un 2>/dev/null)
echo "__LABGPU_SECTION__ uptime"
(uptime -p 2>/dev/null || uptime 2>/dev/null)
echo "__LABGPU_SECTION__ load"
cat /proc/loadavg 2>/dev/null
echo "__LABGPU_SECTION__ nproc"
(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null)
echo "__LABGPU_SECTION__ memory"
if command -v free >/dev/null 2>&1; then
  free -m 2>/dev/null | awk 'NR==2{printf "mem\t%s\t%s\t%s\n",$2,$3,$7} NR==3{printf "swap\t%s\t%s\t%s\n",$2,$3,$4}'
fi
echo "__LABGPU_SECTION__ disks"
for p in __LABGPU_DISK_PATHS__; do
  [ -d "$p" ] || continue
  df -P -h "$p" 2>/dev/null | tail -1
done | awk '!seen[$6]++'
echo "__LABGPU_SECTION__ gpus"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,uuid,name,pci.bus_id,memory.total,memory.used,utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null
else
  echo "NO_NVIDIA_SMI"
fi
echo "__LABGPU_SECTION__ processes"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader,nounits 2>/dev/null | while IFS=, read -r pid uuid mem; do
    pid="$(printf "%s" "$pid" | xargs)"
    uuid="$(printf "%s" "$uuid" | xargs)"
    mem="$(printf "%s" "$mem" | xargs)"
    [ -z "$pid" ] && continue
    user="$(ps -o user= -p "$pid" 2>/dev/null | xargs)"
    ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | xargs)"
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | xargs)"
    etimes="$(ps -o etimes= -p "$pid" 2>/dev/null | xargs)"
    lstart="$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//;s/ *$//')"
    stat="$(ps -o stat= -p "$pid" 2>/dev/null | xargs)"
    pcpu="$(ps -o pcpu= -p "$pid" 2>/dev/null | xargs)"
    pmem="$(ps -o pmem= -p "$pid" 2>/dev/null | xargs)"
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    cmd="$(ps -o command= -p "$pid" 2>/dev/null | tr '\t' ' ' | xargs)"
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" "$pid" "$uuid" "$mem" "$user" "$ppid" "$pgid" "$etimes" "$lstart" "$stat" "$pcpu" "$pmem" "$cwd" "$cmd"
  done
fi
echo "__LABGPU_SECTION__ labgpu"
if command -v labgpu >/dev/null 2>&1; then
  echo "available=1"
  echo "__LABGPU_JSON_STATUS__"
  if command -v timeout >/dev/null 2>&1; then
    timeout 5 labgpu status --json 2>/dev/null
  else
    labgpu status --json 2>/dev/null
  fi
  echo "__LABGPU_JSON_LIST__"
  if command -v timeout >/dev/null 2>&1; then
    timeout 5 labgpu list --json 2>/dev/null
  else
    labgpu list --json 2>/dev/null
  fi
else
  echo "available=0"
fi
"""


SENSITIVE_WORDS = (
    "TOKEN",
    "KEY",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "OPENAI_API_KEY",
    "WANDB_API_KEY",
    "HF_TOKEN",
    "GITHUB_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)

SENSITIVE_NAME_RE = re.compile(r"(?i)(^|[_\-.])(TOKEN|KEY|SECRET|PASSWORD|PASSWD)([_\-.]|$)")


def probe_host(host: SSHHost, *, timeout: int = 8) -> dict[str, Any]:
    started = time.monotonic()
    connect_timeout = max(1, min(int(timeout), 5))
    command_timeout = max(int(timeout) + 16, 24)
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={connect_timeout}",
                "-o",
                "ConnectionAttempts=1",
                "-q",
                host.alias,
                "sh",
                "-s",
            ],
            check=False,
            capture_output=True,
            input=build_remote_script(host.disk_paths),
            text=True,
            timeout=command_timeout,
        )
    except subprocess.TimeoutExpired:
        if ssh_reachable(host, timeout=connect_timeout):
            payload = _base(
                host,
                online=True,
                error="GPU refresh timed out; SSH is reachable.",
                elapsed=time.monotonic() - started,
            )
            payload["mode"] = "stale"
            payload["probe_status"] = "probe_timeout"
            payload["probe_incomplete"] = True
            return payload
        return _base(host, online=False, error="ssh connection timed out", elapsed=time.monotonic() - started)
    except OSError as exc:
        return _base(host, online=False, error=str(exc), elapsed=time.monotonic() - started)

    if result.returncode != 0:
        error = result.stderr.strip() or "GPU refresh failed; SSH is reachable."
        if ssh_reachable(host, timeout=connect_timeout):
            payload = _base(host, online=True, error=error, elapsed=time.monotonic() - started)
            payload["mode"] = "stale"
            payload["probe_status"] = "probe_failed"
            payload["probe_incomplete"] = True
            return payload

    payload = parse_probe_output(result.stdout)
    payload.update(
        _base(
            host,
            online=result.returncode == 0,
            error=None if result.returncode == 0 else (result.stderr.strip() or "ssh failed"),
            elapsed=time.monotonic() - started,
        )
    )
    payload["mode"] = "enhanced" if payload.get("labgpu_available") and payload.get("online") else "agentless"
    if not payload.get("online"):
        payload["mode"] = "offline"
    return payload


def ssh_reachable(host: SSHHost, *, timeout: int = 5) -> bool:
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={max(1, min(int(timeout), 5))}",
                "-o",
                "ConnectionAttempts=1",
                "-q",
                host.alias,
                "true",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=max(timeout + 2, 4),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def parse_probe_output(output: str) -> dict[str, Any]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("__LABGPU_SECTION__ "):
            current = line.removeprefix("__LABGPU_SECTION__ ").strip()
            sections[current] = []
            continue
        if current:
            sections[current].append(line)

    gpus = parse_gpus("\n".join(sections.get("gpus", [])))
    current_user = first_line(sections.get("current_user", []))
    processes = parse_processes("\n".join(sections.get("processes", [])), current_user=current_user)
    by_uuid = {gpu.get("uuid"): gpu for gpu in gpus}
    for proc in processes:
        gpu = by_uuid.get(proc.get("gpu_uuid"))
        if gpu is not None:
            proc["gpu_index"] = gpu.get("index")
            gpu.setdefault("processes", []).append(proc)
    for gpu in gpus:
        gpu.setdefault("processes", [])
    disk_lines = sections.get("disks") or sections.get("disk", [])
    labgpu = parse_labgpu("\n".join(sections.get("labgpu", [])))
    return {
        "remote_hostname": first_line(sections.get("host", [])),
        "current_user": current_user,
        "uptime": first_line(sections.get("uptime", [])),
        "load": first_line(sections.get("load", [])),
        "load_avg": parse_load(first_line(sections.get("load", []))),
        "cpu_cores": to_int(first_line(sections.get("nproc", [])) or ""),
        "memory": parse_memory("\n".join(sections.get("memory", []))),
        "disks": parse_disks("\n".join(disk_lines)),
        "disk": parse_disk(first_line(disk_lines)),
        "gpus": gpus,
        "processes": processes,
        "labgpu_available": labgpu["available"],
        "labgpu_status": labgpu["status"],
        "labgpu_runs": labgpu["runs"],
    }


def parse_gpus(output: str) -> list[dict[str, Any]]:
    if "NO_NVIDIA_SMI" in output:
        return []
    gpus: list[dict[str, Any]] = []
    for row in csv.reader(StringIO(output)):
        row = [cell.strip() for cell in row]
        if len(row) < 8:
            continue
        total = to_int(row[4])
        used = to_int(row[5])
        gpus.append(
            {
                "index": to_int(row[0]),
                "uuid": row[1],
                "name": row[2],
                "pci_bus_id": row[3],
                "memory_total_mb": total,
                "memory_used_mb": used,
                "memory_free_mb": (total - used) if total is not None and used is not None else None,
                "utilization_gpu": to_int(row[6]),
                "temperature": to_int(row[7]),
                "processes": [],
            }
        )
    return gpus


def parse_processes(output: str, *, current_user: str | None = None) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\t", 12)
        if len(parts) < 13:
            continue
        pid, gpu_uuid, memory, user, ppid, pgid, etimes, lstart, stat, pcpu, pmem, cwd, command = parts
        redacted = redact_command(command) if command else None
        processes.append(
            {
                "pid": to_int(pid),
                "gpu_uuid": gpu_uuid,
                "used_memory_mb": to_int(memory),
                "user": user or None,
                "ppid": to_int(ppid),
                "pgid": to_int(pgid),
                "runtime_seconds": to_int(etimes),
                "start_time": lstart or None,
                "state": stat or None,
                "cpu_percent": to_float(pcpu),
                "memory_percent": to_float(pmem),
                "cwd": cwd or None,
                "command": redacted,
                "command_redacted": command != redacted if command else False,
                "command_hash": process_hash(user, lstart, command),
                "is_current_user": bool(current_user and user == current_user),
            }
        )
    return processes


def parse_load(line: str | None) -> dict[str, float | int] | None:
    if not line:
        return None
    parts = line.split()
    if len(parts) < 3:
        return None
    payload: dict[str, float | int] = {}
    for key, raw in (("1m", parts[0]), ("5m", parts[1]), ("15m", parts[2])):
        try:
            payload[key] = float(raw)
        except ValueError:
            pass
    return payload or None


def parse_memory(output: str) -> dict[str, dict[str, int | None]]:
    payload: dict[str, dict[str, int | None]] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        kind, total, used, available = parts[:4]
        payload[kind] = {
            "total_mb": to_int(total),
            "used_mb": to_int(used),
            "available_mb": to_int(available),
            "used_percent": percent(to_int(used), to_int(total)),
        }
    return payload


def parse_disks(output: str) -> list[dict[str, str]]:
    disks: list[dict[str, str]] = []
    for line in output.splitlines():
        disk = parse_disk(line)
        if disk:
            disks.append(disk)
    return disks


def parse_disk(line: str | None) -> dict[str, str] | None:
    if not line:
        return None
    parts = line.split()
    if len(parts) < 6:
        return {"raw": line}
    return {
        "filesystem": parts[0],
        "size": parts[1],
        "used": parts[2],
        "available": parts[3],
        "use_percent": parts[4],
        "mount": parts[5],
    }


def parse_labgpu(output: str) -> dict[str, Any]:
    status_lines: list[str] = []
    runs_lines: list[str] = []
    target: list[str] | None = None
    for line in output.splitlines():
        if line == "__LABGPU_JSON_STATUS__":
            target = status_lines
            continue
        if line == "__LABGPU_JSON_LIST__":
            target = runs_lines
            continue
        if target is not None:
            target.append(line)
    return {
        "available": "available=1" in output,
        "status": parse_json_block(status_lines),
        "runs": parse_json_block(runs_lines),
    }


def parse_json_block(lines: list[str]) -> Any:
    text = "\n".join(line for line in lines if line.strip())
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def first_line(lines: list[str] | None) -> str | None:
    if not lines:
        return None
    for line in lines:
        if line.strip():
            return line.strip()
    return None


def to_int(value: str) -> int | None:
    value = value.strip()
    if value in {"", "N/A", "[Not Supported]"}:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def to_float(value: str) -> float | None:
    value = value.strip()
    if value in {"", "N/A", "[Not Supported]"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def percent(used: int | None, total: int | None) -> int | None:
    if used is None or not total:
        return None
    return round((used / total) * 100)


def redact_command(command: str) -> str:
    text = command
    env_pattern = re.compile(r"\b([A-Za-z0-9_.-]+)=([^\s]+)")
    flag_pattern = re.compile(r"(--?[A-Za-z0-9_.-]+)(=|\s+)([^\s]+)")

    def redact_env(match: re.Match[str]) -> str:
        name = match.group(1)
        if is_sensitive_name(name):
            return f"{name}=<redacted>"
        return match.group(0)

    def redact_flag(match: re.Match[str]) -> str:
        name = match.group(1).lstrip("-")
        if is_sensitive_name(name):
            return f"{match.group(1)}{match.group(2)}<redacted>"
        return match.group(0)

    text = env_pattern.sub(redact_env, text)
    text = flag_pattern.sub(redact_flag, text)
    for word in SENSITIVE_WORDS:
        text = re.sub(fr"(?i)\b{re.escape(word)}\b\s*=\s*[^\s]+", f"{word}=<redacted>", text)
    return text


def is_sensitive_name(name: str) -> bool:
    upper = name.upper()
    return upper in SENSITIVE_WORDS or bool(SENSITIVE_NAME_RE.search(upper))


def process_hash(user: str | None, start_time: str | None, command: str | None) -> str:
    raw = "\n".join([user or "", start_time or "", command or ""])
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _base(host: SSHHost, *, online: bool, error: str | None, elapsed: float) -> dict[str, Any]:
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
        "online": online,
        "error": error,
        "elapsed_ms": int(elapsed * 1000),
        "probed_at": now_utc(),
    }


def build_remote_script(disk_paths: list[str] | None = None) -> str:
    paths = disk_paths or DEFAULT_DISK_PATHS
    return REMOTE_SCRIPT_TEMPLATE.replace("__LABGPU_DISK_PATHS__", " ".join(shlex.quote(path) for path in paths))
