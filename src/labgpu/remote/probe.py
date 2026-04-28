from __future__ import annotations

import csv
import json
import re
import subprocess
import time
from io import StringIO
from typing import Any

from labgpu.remote.ssh_config import SSHHost
from labgpu.utils.time import now_utc


REMOTE_SCRIPT = r"""
set +e
echo "__LABGPU_SECTION__ host"
hostname 2>/dev/null
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
for p in / /home /data /scratch /mnt /nvme; do
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
    cmd="$(ps -o command= -p "$pid" 2>/dev/null | tr '\t' ' ' | xargs)"
    printf "%s\t%s\t%s\t%s\t%s\n" "$pid" "$uuid" "$mem" "$user" "$cmd"
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
                "-s",
            ],
            check=False,
            capture_output=True,
            input=REMOTE_SCRIPT,
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
    payload["mode"] = "enhanced" if payload.get("labgpu_available") and payload.get("online") else "agentless"
    if not payload.get("online"):
        payload["mode"] = "offline"
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
    disk_lines = sections.get("disks") or sections.get("disk", [])
    labgpu = parse_labgpu("\n".join(sections.get("labgpu", [])))
    return {
        "remote_hostname": first_line(sections.get("host", [])),
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
                "command": redact_command(command) if command else None,
                "command_redacted": command != redact_command(command) if command else False,
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


def percent(used: int | None, total: int | None) -> int | None:
    if used is None or not total:
        return None
    return round((used / total) * 100)


def redact_command(command: str) -> str:
    text = command
    env_pattern = re.compile(
        r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)=([^\s]+)"
    )
    flag_pattern = re.compile(
        r"(?i)(--?[A-Z0-9_.-]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)[A-Z0-9_.-]*)(=|\s+)([^\s]+)"
    )
    text = env_pattern.sub(r"\1=<redacted>", text)
    text = flag_pattern.sub(r"\1\2<redacted>", text)
    for word in SENSITIVE_WORDS:
        text = re.sub(fr"(?i)\b{re.escape(word)}\b\s*=\s*[^\s]+", f"{word}=<redacted>", text)
    return text


def _base(host: SSHHost, *, online: bool, error: str | None, elapsed: float) -> dict[str, Any]:
    return {
        "alias": host.alias,
        "hostname": host.hostname,
        "user": host.user,
        "port": host.port,
        "proxyjump": host.proxyjump,
        "online": online,
        "error": error,
        "elapsed_ms": int(elapsed * 1000),
        "probed_at": now_utc(),
    }
