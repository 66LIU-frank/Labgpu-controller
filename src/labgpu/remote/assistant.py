from __future__ import annotations

import re
from typing import Any

from labgpu.remote import ranking
from labgpu.remote.workspace import failure_inbox_items, training_items


MODEL_HINTS = ("A100", "H100", "H800", "4090", "3090", "A6000", "L40", "V100")


def assistant_reply(data: dict[str, object], message: str) -> dict[str, object]:
    text = message.strip()
    if not text:
        return {"ok": False, "intent": "empty", "reply": "Ask LabGPU what you want to do: find a GPU, locate a run, explain a failure, or build a launch plan."}

    lowered = text.lower()
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    hosts = data.get("hosts") if isinstance(data.get("hosts"), list) else []
    if wants_failures(lowered):
        return failure_reply(hosts, overview)
    if wants_where(lowered):
        return where_reply(hosts, overview)
    if wants_context(lowered):
        return context_reply(hosts, overview, text)
    return gpu_reply(overview, text)


def gpu_reply(overview: dict[str, object], message: str) -> dict[str, object]:
    min_vram = extract_min_vram(message)
    prefer = extract_model_hint(message)
    command = extract_command(message)
    ui: dict[str, object] = {"availability": "available"}
    if min_vram:
        ui["min_vram"] = min_vram
    if prefer:
        ui["model"] = prefer
        ui["prefer"] = prefer
    choices = ranking.filter_gpu_items(overview.get("gpu_items") or [], ui)
    if not choices:
        busy = overview.get("busy_gpus", 0)
        idle = overview.get("suspected_idle_gpus", 0)
        reply = "\n".join(
            [
                "I could not find a clearly free GPU matching that request.",
                f"Busy GPUs: {busy}. Suspected idle GPUs: {idle}.",
                "Try lowering `--min-vram`, removing the model preference, or checking Problems.",
            ]
        )
        return {"ok": True, "intent": "rank_gpus", "reply": reply, "copy": "labgpu pick --all --explain"}

    best = choices[0]
    rec = ranking.gpu_recommendation(best, prefer=prefer)
    reasons = ranking.gpu_recommendation_reasons(best, rec, prefer=prefer)
    snippet = ranking.launch_snippet(best, command=command)
    server = best.get("server") or "-"
    gpu = best.get("index")
    model = best.get("name") or "-"
    free = ranking.format_memory(best.get("memory_free_mb"))
    lines = [
        f"Recommended: {server} GPU {gpu} ({model})",
        f"Free VRAM: {free}. Label: {rec['label']}. Score: {rec['score']}.",
        "",
        "Why:",
        *[f"- {reason}" for reason in reasons],
        "",
        "Copyable launch plan:",
        snippet,
    ]
    return {
        "ok": True,
        "intent": "rank_gpus",
        "reply": "\n".join(lines),
        "copy": snippet,
        "recommendation": {
            "server": server,
            "gpu": gpu,
            "model": model,
            "free_memory": free,
            "label": rec["label"],
        },
    }


def where_reply(hosts: list[object], overview: dict[str, object]) -> dict[str, object]:
    items = training_items(hosts, overview)
    rows = [item for item in items if isinstance(item, dict)]
    if not rows:
        return {"ok": True, "intent": "where", "reply": "I do not see any LabGPU run or own GPU process yet.", "copy": "labgpu where"}
    lines = ["Your visible training work:"]
    for item in rows[:8]:
        lines.append(
            f"- {item.get('host') or '-'} GPU {item.get('gpu') or '-'} · {item.get('name') or '-'} · "
            f"PID {item.get('pid') or '-'} · {item.get('status') or '-'} · {item.get('health') or '-'}"
        )
    return {"ok": True, "intent": "where", "reply": "\n".join(lines), "copy": "labgpu where"}


def failure_reply(hosts: list[object], overview: dict[str, object]) -> dict[str, object]:
    items = failure_inbox_items(hosts, overview)
    rows = [item for item in items if isinstance(item, dict)]
    if not rows:
        return {"ok": True, "intent": "failures", "reply": "No failed or suspicious run is visible right now.", "copy": "labgpu where"}
    lines = ["Failed or suspicious items:"]
    for item in rows[:8]:
        lines.append(
            f"- {item.get('host') or '-'} · {item.get('name') or item.get('health') or '-'} · "
            f"{item.get('status') or '-'} · {item.get('diagnosis') or '-'}"
        )
    first = rows[0]
    copy = "labgpu diagnose " + str(first.get("name")) if first.get("kind") == "run" and first.get("name") else "labgpu where"
    return {"ok": True, "intent": "failures", "reply": "\n".join(lines), "copy": copy}


def context_reply(hosts: list[object], overview: dict[str, object], message: str) -> dict[str, object]:
    items = [item for item in training_items(hosts, overview) if isinstance(item, dict)]
    target = find_named_item(items, message)
    if not target:
        return {
            "ok": True,
            "intent": "context",
            "reply": "Tell me the run name, or use `labgpu where` first to find it.",
            "copy": "labgpu where",
        }
    name = str(target.get("name") or "")
    if target.get("kind") == "run" and name:
        command = f"labgpu context {name} --copy"
        return {"ok": True, "intent": "context", "reply": f"Use this to copy debug context for `{name}`:\n{command}", "copy": command}
    process = target.get("process") if isinstance(target.get("process"), dict) else target
    copy = "\n".join(
        [
            "LabGPU process context",
            f"host: {target.get('host') or '-'}",
            f"gpu: {target.get('gpu') or '-'}",
            f"pid: {target.get('pid') or '-'}",
            f"health: {target.get('health') or '-'}",
            f"diagnosis: {target.get('diagnosis') or '-'}",
            f"command: {process.get('command') or target.get('command') or '-'}",
            "",
            "Adopt command:",
            f"labgpu adopt {target.get('pid')} --name NAME --gpu {target.get('gpu') or ''} --log ./train.log",
        ]
    )
    return {"ok": True, "intent": "context", "reply": "This process is not a LabGPU run yet. Copy this process context or adopt it first.", "copy": copy}


def wants_where(text: str) -> bool:
    return any(word in text for word in ("where", "在哪", "哪里", "找回", "任务", "job", "run 在"))


def wants_failures(text: str) -> bool:
    return any(word in text for word in ("failed", "failure", "fail", "失败", "挂", "报错", "oom", "traceback", "nccl", "disk full", "可疑"))


def wants_context(text: str) -> bool:
    return any(word in text for word in ("context", "debug", "求助", "发给", "贴给", "chatgpt"))


def extract_min_vram(message: str) -> str | None:
    patterns = [
        r"(\d+(?:\.\d+)?)\s*(?:gb|g)\b",
        r"(\d+(?:\.\d+)?)\s*(?:mb|m)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            unit = "G" if "g" in match.group(0).lower() else "M"
            return f"{match.group(1)}{unit}"
    return None


def extract_model_hint(message: str) -> str | None:
    lowered = message.lower()
    for model in MODEL_HINTS:
        if model.lower() in lowered:
            return model
    return None


def extract_command(message: str) -> str:
    match = re.search(r"((?:python|bash|torchrun|accelerate|deepspeed)\s+.+)$", message.strip(), flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return "python train.py"


def find_named_item(items: list[dict[str, object]], message: str) -> dict[str, object] | None:
    lowered = message.lower()
    for item in items:
        name = str(item.get("name") or "")
        if name and name.lower() in lowered:
            return item
    return items[0] if len(items) == 1 else None
