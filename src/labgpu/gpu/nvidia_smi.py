from __future__ import annotations

import csv
import platform
import shutil
import subprocess
from io import StringIO
from typing import Any

from labgpu.gpu.collector import GPUCollector
from labgpu.process.inspector import inspect_process


class NvidiaSmiCollector(GPUCollector):
    def collect(self) -> dict[str, Any]:
        if not shutil.which("nvidia-smi"):
            return {
                "available": False,
                "source": "nvidia-smi",
                "host": platform.node() or "localhost",
                "error": "nvidia-smi not found",
                "gpus": [],
                "processes": [],
            }
        try:
            gpus = parse_gpu_query(_nvidia_smi([
                "--query-gpu=index,uuid,name,pci.bus_id,memory.total,memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ]))
            processes = parse_process_query(_nvidia_smi([
                "--query-compute-apps=pid,gpu_uuid,used_memory",
                "--format=csv,noheader,nounits",
            ], allow_failure=True))
        except RuntimeError as exc:
            return {
                "available": False,
                "source": "nvidia-smi",
                "host": platform.node() or "localhost",
                "error": str(exc),
                "gpus": [],
                "processes": [],
            }

        by_uuid = {gpu["uuid"]: gpu for gpu in gpus}
        for proc in processes:
            proc.update(inspect_process(int(proc["pid"])))
            gpu = by_uuid.get(proc.get("gpu_uuid"))
            if gpu:
                gpu.setdefault("processes", []).append(proc)
        for gpu in gpus:
            gpu.setdefault("processes", [])
        return {
            "available": True,
            "source": "nvidia-smi",
            "host": platform.node() or "localhost",
            "error": None,
            "gpus": gpus,
            "processes": processes,
        }


def parse_gpu_query(output: str) -> list[dict[str, Any]]:
    gpus = []
    for row in _rows(output):
        if len(row) < 8:
            continue
        gpus.append(
            {
                "index": _to_int(row[0]),
                "uuid": row[1],
                "name": row[2],
                "pci_bus_id": row[3],
                "memory_total_mb": _to_int(row[4]),
                "memory_used_mb": _to_int(row[5]),
                "utilization_gpu": _to_int(row[6]),
                "temperature": _to_int(row[7]),
                "processes": [],
            }
        )
    return gpus


def parse_process_query(output: str) -> list[dict[str, Any]]:
    processes = []
    for row in _rows(output):
        if len(row) < 3 or not row[0]:
            continue
        pid = _to_int(row[0])
        if pid is None:
            continue
        processes.append(
            {
                "pid": pid,
                "gpu_uuid": row[1],
                "used_memory_mb": _to_int(row[2]),
            }
        )
    return processes


def _nvidia_smi(args: list[str], *, allow_failure: bool = False) -> str:
    result = subprocess.run(
        ["nvidia-smi", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and not allow_failure:
        raise RuntimeError(result.stderr.strip() or "nvidia-smi failed")
    if result.returncode != 0:
        return ""
    return result.stdout


def _rows(output: str) -> list[list[str]]:
    return [[cell.strip() for cell in row] for row in csv.reader(StringIO(output)) if row]


def _to_int(value: str) -> int | None:
    if value in {"", "N/A", "[Not Supported]"}:
        return None
    try:
        return int(value)
    except ValueError:
        return None
