from __future__ import annotations

from typing import Any

from labgpu.gpu.fake import FakeCollector
from labgpu.gpu.nvidia_smi import NvidiaSmiCollector


FREE_MEMORY_USED_THRESHOLD_MB = 512


def collect_local_gpu_payload(*, fake: bool = False) -> dict[str, Any]:
    collector = FakeCollector() if fake else NvidiaSmiCollector()
    return collector.collect()


def pick_local_gpu(
    *,
    min_vram_mb: int | None = None,
    prefer: str | None = None,
    payload: dict[str, Any] | None = None,
    fake: bool = False,
) -> dict[str, Any]:
    payload = payload or collect_local_gpu_payload(fake=fake)
    if not payload.get("available"):
        raise RuntimeError(str(payload.get("error") or "local GPU status is unavailable"))

    preferred = [part.strip().lower() for part in str(prefer or "").replace("/", ",").split(",") if part.strip()]
    candidates: list[dict[str, Any]] = []
    for raw in payload.get("gpus") or []:
        if not isinstance(raw, dict):
            continue
        gpu = dict(raw)
        free_mb = gpu_free_memory_mb(gpu)
        gpu["memory_free_mb"] = free_mb
        if min_vram_mb is not None and free_mb < min_vram_mb:
            continue
        if not is_gpu_free(gpu):
            continue
        gpu["_labgpu_score"] = local_gpu_score(gpu, preferred=preferred)
        candidates.append(gpu)

    if not candidates:
        requirement = f" with at least {min_vram_mb} MB free VRAM" if min_vram_mb else ""
        raise RuntimeError(f"no free local GPU found{requirement}")

    candidates.sort(key=lambda gpu: (gpu["_labgpu_score"], gpu.get("memory_free_mb") or 0), reverse=True)
    winner = dict(candidates[0])
    winner.pop("_labgpu_score", None)
    return winner


def detect_pid_gpus(
    pid: int,
    *,
    payload: dict[str, Any] | None = None,
    fake: bool = False,
) -> list[str]:
    payload = payload or collect_local_gpu_payload(fake=fake)
    if not payload.get("available"):
        return []

    by_uuid = {
        str(gpu.get("uuid")): gpu
        for gpu in payload.get("gpus") or []
        if isinstance(gpu, dict) and gpu.get("uuid")
    }
    indices: list[str] = []

    for proc in payload.get("processes") or []:
        if not isinstance(proc, dict) or int(proc.get("pid") or -1) != pid:
            continue
        gpu = by_uuid.get(str(proc.get("gpu_uuid") or ""))
        if gpu is not None:
            _append_unique(indices, gpu.get("index"))

    for gpu in payload.get("gpus") or []:
        if not isinstance(gpu, dict):
            continue
        for proc in gpu.get("processes") or []:
            if isinstance(proc, dict) and int(proc.get("pid") or -1) == pid:
                _append_unique(indices, gpu.get("index"))

    return indices


def is_gpu_free(gpu: dict[str, Any]) -> bool:
    processes = [proc for proc in gpu.get("processes") or [] if isinstance(proc, dict)]
    used_mb = _to_int(gpu.get("memory_used_mb"))
    return not processes and (used_mb is None or used_mb <= FREE_MEMORY_USED_THRESHOLD_MB)


def gpu_free_memory_mb(gpu: dict[str, Any]) -> int:
    total = _to_int(gpu.get("memory_total_mb"))
    used = _to_int(gpu.get("memory_used_mb")) or 0
    if total is None:
        return 0
    return max(0, total - used)


def local_gpu_score(gpu: dict[str, Any], *, preferred: list[str]) -> int:
    score = gpu_free_memory_mb(gpu)
    name = str(gpu.get("name") or "").lower()
    if any(part in name for part in preferred):
        score += 100_000
    if any(model in name for model in ("a100", "h100", "h800")):
        score += 20_000
    elif "4090" in name:
        score += 10_000
    util = _to_int(gpu.get("utilization_gpu"))
    if util is not None:
        score -= util * 128
    return score


def _append_unique(values: list[str], value: object) -> None:
    if value is None:
        return
    text = str(value)
    if text not in values:
        values.append(text)


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
