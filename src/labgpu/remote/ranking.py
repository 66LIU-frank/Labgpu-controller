from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GpuRecommendation:
    host: str
    gpu_index: str
    model: str
    memory_free_mb: int
    memory_total_mb: int | None
    utilization_gpu: int | None
    server_load: Any
    disk_health: str
    tags: list[str]
    alerts: list[dict[str, object]]
    rank: str
    score: float
    reasons: list[str]
    ssh_command: str
    cuda_command: str
    launch_snippet: str


def rank_gpus(
    hosts: list[dict[str, object]],
    *,
    min_vram_mb: int | None = None,
    prefer: str | None = None,
    tag: str | None = None,
    cmd: str | None = None,
) -> list[GpuRecommendation]:
    from labgpu.remote.state import build_overview

    overview = build_overview(hosts)
    ui: dict[str, object] = {
        "availability": "all",
        "prefer": prefer or "",
        "model": prefer or "",
        "tag": tag or "",
    }
    if min_vram_mb is not None:
        ui["min_vram"] = f"{min_vram_mb}M"
    items = filter_gpu_items(overview.get("gpu_items") or [], ui)
    return [recommendation_from_item(item, command=cmd or "python train.py", prefer=prefer) for item in items]


def recommendation_from_item(item: dict[str, object], *, command: str = "python train.py", prefer: str | None = None) -> GpuRecommendation:
    rec = gpu_recommendation(item, prefer=prefer)
    labels = {"Recommended": "recommended", "OK": "ok", "Busy": "busy", "Not recommended": "not_recommended"}
    gpu = str(item.get("cuda_visible_devices") or item.get("index") or "")
    tags = item.get("tags") or item.get("server_tags") or []
    alerts = item.get("server_alerts") if isinstance(item.get("server_alerts"), list) else []
    return GpuRecommendation(
        host=str(item.get("server") or ""),
        gpu_index=str(item.get("index") or gpu),
        model=str(item.get("name") or ""),
        memory_free_mb=int(item.get("memory_free_mb") or 0),
        memory_total_mb=int(item["memory_total_mb"]) if item.get("memory_total_mb") is not None else None,
        utilization_gpu=int(item["utilization_gpu"]) if item.get("utilization_gpu") is not None else None,
        server_load=item.get("load"),
        disk_health=str(item.get("disk_health") or "unknown"),
        tags=[str(value) for value in tags] if isinstance(tags, list) else [],
        alerts=[alert for alert in alerts if isinstance(alert, dict)],
        rank=labels.get(rec["label"], "ok"),
        score=float(rec["score"]),
        reasons=[rec["reason"]],
        ssh_command=str(item.get("ssh_command") or f"ssh {item.get('server')}"),
        cuda_command=f"CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES={gpu}",
        launch_snippet=launch_snippet(item, command=command),
    )


def filter_available_gpu_items(items: object, ui: dict[str, object]) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    q = str(ui.get("q") or "").lower()
    model = str(ui.get("model") or "").lower()
    tag = str(ui.get("tag") or "").lower()
    sort = str(ui.get("sort") or "")
    min_mem_mb = requested_vram_mb(ui)
    filtered: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        haystack = " ".join(
            [
                str(item.get("server") or ""),
                str(item.get("name") or ""),
                join_values(item.get("tags") or []),
            ]
        ).lower()
        if q and q not in haystack:
            continue
        if model and model not in str(item.get("name") or "").lower():
            continue
        if tag and tag not in join_values(item.get("tags") or []).lower():
            continue
        if min_mem_mb is not None and int(item.get("memory_free_mb") or 0) < min_mem_mb:
            continue
        filtered.append(dict(item))
    if sort == "model":
        filtered.sort(key=lambda item: (str(item.get("name") or ""), str(item.get("server") or "")))
    elif sort == "load":
        filtered.sort(key=lambda item: load_sort_key(item.get("load")))
    else:
        filtered.sort(key=lambda item: int(item.get("memory_free_mb") or 0), reverse=True)
    return filtered


def filter_gpu_items(items: object, ui: dict[str, object]) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    q = str(ui.get("q") or "").lower()
    model = str(ui.get("model") or "").lower()
    tag = str(ui.get("tag") or "").lower()
    availability = str(ui.get("availability") or "available")
    min_mem_mb = requested_vram_mb(ui)
    filtered: list[dict[str, object]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item.setdefault("ssh_command", f"ssh {item.get('server')}")
        item.setdefault("cuda_visible_devices", str(item.get("index")))
        haystack = " ".join(
            [
                str(item.get("server") or ""),
                str(item.get("name") or ""),
                join_values(item.get("tags") or item.get("server_tags") or []),
                str(item.get("availability") or ""),
            ]
        ).lower()
        if q and q not in haystack:
            continue
        if model and model not in str(item.get("name") or "").lower():
            continue
        if tag and tag not in join_values(item.get("tags") or item.get("server_tags") or []).lower():
            continue
        if min_mem_mb is not None and int(item.get("memory_free_mb") or 0) < min_mem_mb:
            continue
        item_availability = str(item.get("availability") or item.get("status") or "")
        if availability in {"", "available"} and item_availability not in {"free", "probably_available"}:
            continue
        if availability == "busy" and item_availability != "busy":
            continue
        if availability == "idle" and item_availability != "idle_but_occupied":
            continue
        filtered.append(item)
    filtered.sort(key=lambda item: gpu_recommendation_sort_key(item, prefer=ui.get("prefer")))
    return filtered


def requested_vram_mb(ui: dict[str, object]) -> int | None:
    value = ui.get("min_vram") or ui.get("min_mem_gb")
    if value is None:
        return None
    return parse_vram_to_mb(str(value))


def parse_vram_to_mb(value: str) -> int | None:
    text = value.strip().lower().replace(" ", "")
    if not text:
        return None
    multiplier = 1024
    if text.endswith(("gb", "g")):
        text = text.removesuffix("gb").removesuffix("g")
        multiplier = 1024
    elif text.endswith(("mb", "m")):
        text = text.removesuffix("mb").removesuffix("m")
        multiplier = 1
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def gpu_recommendation(item: dict[str, object], *, prefer: object = None) -> dict[str, str]:
    availability = str(item.get("availability") or item.get("status") or "")
    disk = str(item.get("disk_health") or "unknown")
    free_mb = int(item.get("memory_free_mb") or 0)
    load_ratio = load_ratio_value(item.get("load"))
    score = recommendation_score(item, prefer=prefer)
    alert_penalty = alert_severity(item)
    if availability == "busy":
        return {"label": "Busy", "class": "busy", "severity": "warning", "reason": "A compute process is using this GPU.", "score": str(score)}
    if availability == "idle_but_occupied":
        return {
            "label": "Not recommended",
            "class": "not-recommended",
            "severity": "warning",
            "reason": "GPU memory is occupied with low current utilization.",
            "score": str(score),
        }
    if disk == "critical" or alert_penalty >= 20:
        return {"label": "Not recommended", "class": "not-recommended", "severity": "error", "reason": "Server has a critical health alert.", "score": str(score)}
    if disk == "warning" or load_ratio >= 0.85 or alert_penalty:
        return {"label": "OK", "class": "ok-choice", "severity": "warning", "reason": "Usable, but server health has warnings.", "score": str(score)}
    if free_mb >= 40 * 1024:
        return {"label": "Recommended", "class": "recommended", "severity": "ok", "reason": "High free memory and no major server warning.", "score": str(score)}
    return {"label": "OK", "class": "ok-choice", "severity": "ok", "reason": "Free GPU with no major warning.", "score": str(score)}


def recommendation_score(item: dict[str, object], *, prefer: object = None) -> int:
    availability = str(item.get("availability") or item.get("status") or "")
    score = 0
    if availability in {"free", "probably_available"}:
        score += 50
    elif availability == "idle_but_occupied":
        score += 15
    free_gb = int(item.get("memory_free_mb") or 0) / 1024
    score += min(30, int(free_gb / 4))
    name = str(item.get("name") or "").lower()
    if any(model in name for model in ("a100", "h100", "h800")):
        score += 12
    elif "4090" in name:
        score += 8
    preferred = [part.strip().lower() for part in str(prefer or "").replace("/", ",").split(",") if part.strip()]
    if preferred and any(part in name for part in preferred):
        score += 12
    disk = str(item.get("disk_health") or "")
    if disk == "critical":
        score -= 40
    elif disk == "warning":
        score -= 15
    score -= alert_severity(item)
    score -= int(load_ratio_value(item.get("load")) * 20)
    if availability == "busy":
        score -= 60
    return max(0, min(100, score))


def alert_severity(item: dict[str, object]) -> int:
    alerts = item.get("server_alerts")
    if not isinstance(alerts, list):
        return 0
    penalty = 0
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        severity = str(alert.get("severity") or "")
        if severity == "error":
            penalty += 20
        elif severity == "warning":
            penalty += 6
    return penalty


def gpu_recommendation_sort_key(item: dict[str, object], *, prefer: object = None) -> tuple[int, int, float, int]:
    recommendation = gpu_recommendation(item, prefer=prefer)
    rank = {"Recommended": 0, "OK": 1, "Not recommended": 2, "Busy": 3}.get(recommendation["label"], 4)
    score = int(recommendation["score"])
    load = load_ratio_value(item.get("load"))
    free = int(item.get("memory_free_mb") or 0)
    return (rank, -score, load, -free)


def launch_snippet(item: dict[str, object], *, command: str = "python train.py", run_name: str = "NAME") -> str:
    server = str(item.get("server") or "")
    gpu = str(item.get("cuda_visible_devices") or item.get("index") or "")
    if not server:
        return f"CUDA_VISIBLE_DEVICES={gpu} labgpu run --name {run_name} --gpu {gpu} -- {command}"
    return f"ssh {server} 'labgpu run --name {run_name} --gpu {gpu} -- {command}'"


def load_sort_key(value: object) -> tuple[float, str]:
    if isinstance(value, dict):
        raw = value.get("1m")
        try:
            return (float(raw), "")
        except (TypeError, ValueError):
            pass
    return (999999.0, "")


def load_ratio_value(value: object) -> float:
    if isinstance(value, dict):
        raw = value.get("ratio")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def load_value(value: object) -> str:
    if isinstance(value, dict):
        raw = value.get("1m")
        if raw is not None:
            return str(raw)
    return "-"


def format_memory(value: object) -> str:
    try:
        mb = float(value)
    except (TypeError, ValueError):
        return "-"
    if mb >= 1024:
        gb = mb / 1024
        return f"{gb:.1f} GB"
    return f"{int(mb)} MB"


def join_values(values: object) -> str:
    if not isinstance(values, list):
        return "-"
    return ",".join(str(value) for value in values) or "-"
