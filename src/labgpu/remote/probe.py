from __future__ import annotations

import csv
import subprocess
import time
from io import StringIO
from typing import Any

from labgpu.remote.ssh_config import SSHHost


REMOTE_SCRIPT = r"""
set +e
echo "__LABGPU_SECTION__ host"
hostname 2>/dev/null
echo "__LABGPU_SECTION__ uptime"
(uptime -p 2>/dev/null || uptime 2>/dev/null)
echo "__LABGPU_SECTION__ load"
cat /proc/loadavg 2>/dev/null
echo "__LABGPU_SECTION__ disk"
df -P -h / 2>/dev/null | tail -1
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
    cmd="$(ps -o command= -p "$pid" 2>/dev/null | tr '\t' ' ' | xargs)"
    printf "%s\t%s\t%s\t%s\t%s\n" "$pid" "$uuid" "$mem" "$user" "$cmd"
  done
fi
"""


def probe_host(host: SSHHost, *, timeout: int = 8) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={timeout}",
                "-q",
                host.alias,
                "sh",
                "-lc",
                REMOTE_SCRIPT,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=max(timeout + 4, 6),
        )
    except subprocess.TimeoutExpired:
        return _base(host, online=False, error="ssh probe timed out", elapsed=time.monotonic() - started)
    except OSError as exc:
        return _base(host, online=False, error=str(exc), elapsed=time.monotonic() - started)

    payload = parse_probe_output(result.stdout)
    payload.update(
        _base(
            host,
            online=result.returncode == 0,
            error=None if result.returncode == 0 else (result.stderr.strip() or "ssh failed"),
            elapsed=time.monotonic() - started,
        )
    )
    return payload


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
    processes = parse_processes("\n".join(sections.get("processes", [])))
    by_uuid = {gpu.get("uuid"): gpu for gpu in gpus}
    for proc in processes:
        gpu = by_uuid.get(proc.get("gpu_uuid"))
        if gpu is not None:
            gpu.setdefault("processes", []).append(proc)
    for gpu in gpus:
        gpu.setdefault("processes", [])
    return {
        "remote_hostname": first_line(sections.get("host", [])),
        "uptime": first_line(sections.get("uptime", [])),
        "load": first_line(sections.get("load", [])),
        "disk": parse_disk(first_line(sections.get("disk", []))),
        "gpus": gpus,
        "processes": processes,
    }


def parse_gpus(output: str) -> list[dict[str, Any]]:
    if "NO_NVIDIA_SMI" in output:
        return []
    gpus: list[dict[str, Any]] = []
    for row in csv.reader(StringIO(output)):
        row = [cell.strip() for cell in row]
        if len(row) < 8:
            continue
        gpus.append(
            {
                "index": to_int(row[0]),
                "uuid": row[1],
                "name": row[2],
                "pci_bus_id": row[3],
                "memory_total_mb": to_int(row[4]),
                "memory_used_mb": to_int(row[5]),
                "utilization_gpu": to_int(row[6]),
                "temperature": to_int(row[7]),
                "processes": [],
            }
        )
    return gpus


def parse_processes(output: str) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\t", 4)
        if len(parts) < 5:
            continue
        pid, gpu_uuid, memory, user, command = parts
        processes.append(
            {
                "pid": to_int(pid),
                "gpu_uuid": gpu_uuid,
                "used_memory_mb": to_int(memory),
                "user": user or None,
                "command": command or None,
            }
        )
    return processes


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


def _base(host: SSHHost, *, online: bool, error: str | None, elapsed: float) -> dict[str, Any]:
    return {
        "alias": host.alias,
        "hostname": host.hostname,
        "user": host.user,
        "port": host.port,
        "online": online,
        "error": error,
        "elapsed_ms": int(elapsed * 1000),
    }
