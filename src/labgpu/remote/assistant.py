from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from labgpu.remote import ranking
from labgpu.remote.probe import redact_command
from labgpu.remote.workspace import failure_inbox_items, training_items


MODEL_HINTS = ("A100", "H100", "H800", "4090", "3090", "A6000", "L40", "V100")
MAX_CONTEXT_CHARS = 16_000


def assistant_reply(data: dict[str, object], message: str, options: dict[str, object] | None = None) -> dict[str, object]:
    local = local_assistant_reply(data, message)
    if not wants_api_assistant(options):
        local["mode"] = "local"
        return local
    return api_assistant_reply(data, message, local, options or {})


def local_assistant_reply(data: dict[str, object], message: str) -> dict[str, object]:
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


def wants_api_assistant(options: dict[str, object] | None) -> bool:
    if not isinstance(options, dict):
        return False
    mode = str(options.get("mode") or "").strip().lower()
    return mode in {"api", "llm", "external"}


def api_assistant_reply(data: dict[str, object], message: str, local: dict[str, object], options: dict[str, object]) -> dict[str, object]:
    api_url = normalize_api_url(str(options.get("api_url") or options.get("base_url") or ""))
    api_key = str(options.get("api_key") or "").strip()
    model = str(options.get("model") or "").strip()
    if not api_url or not model:
        local = dict(local)
        local["mode"] = "local"
        local["api_error"] = "Assistant API mode needs an API URL and model. Falling back to local LabGPU rules."
        local["reply"] = f"{local['api_error']}\n\n{local.get('reply') or ''}"
        return local
    context = build_assistant_context(data, local)
    messages = [
        {"role": "system", "content": assistant_system_prompt()},
        {"role": "user", "content": f"User request:\n{message.strip()}\n\nLabGPU workspace context:\n{context}"},
    ]
    try:
        content = call_chat_completion(api_url=api_url, api_key=api_key, model=model, messages=messages)
    except AssistantAPIError as exc:
        fallback = dict(local)
        fallback["mode"] = "local"
        fallback["api_error"] = str(exc)
        fallback["reply"] = f"Assistant API request failed: {exc}\n\nLocal LabGPU fallback:\n{local.get('reply') or ''}"
        return fallback
    result = dict(local)
    result["ok"] = True
    result["mode"] = "api"
    result["reply"] = content.strip() or str(local.get("reply") or "")
    return result


def assistant_system_prompt() -> str:
    return "\n".join(
        [
            "You are LabGPU Assistant, a read-only and copy-only helper for a personal GPU workspace.",
            "Use the provided LabGPU workspace context as ground truth.",
            "Help students find GPUs, locate their runs, diagnose visible failures, and generate copyable SSH/CUDA/LabGPU commands.",
            "Do not claim that you executed anything.",
            "Do not ask for or expose API keys, passwords, tokens, or private SSH keys.",
            "Do not suggest arbitrary destructive shell commands.",
            "For launch/adopt/stop actions, provide a plan and copyable commands; require the user to confirm in LabGPU UI/CLI.",
            "Answer in the same language as the user when possible.",
        ]
    )


class AssistantAPIError(RuntimeError):
    pass


def normalize_api_url(value: str) -> str:
    text = value.strip().rstrip("/")
    if not text:
        return ""
    if text.endswith("/chat/completions"):
        return text
    if text.endswith("/v1"):
        return f"{text}/chat/completions"
    return text


def call_chat_completion(
    *,
    api_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: int = 30,
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(api_url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - user-configured local UI feature
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AssistantAPIError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise AssistantAPIError(str(exc.reason)) from exc
    except OSError as exc:
        raise AssistantAPIError(str(exc)) from exc
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise AssistantAPIError("API returned non-JSON response") from exc
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    raise AssistantAPIError("API response did not contain assistant text")


def build_assistant_context(data: dict[str, object], local: dict[str, object]) -> str:
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    hosts = data.get("hosts") if isinstance(data.get("hosts"), list) else []
    context = {
        "overview": {
            "online_servers": overview.get("online_servers"),
            "total_servers": overview.get("total_servers"),
            "available_gpus": overview.get("available_gpus"),
            "total_gpus": overview.get("total_gpus"),
            "my_processes": overview.get("my_processes"),
            "alerts": overview.get("alerts"),
            "critical_alerts": overview.get("critical_alerts"),
            "warning_alerts": overview.get("warning_alerts"),
        },
        "top_gpu_choices": compact_gpu_choices(overview.get("gpu_items") or []),
        "my_training": compact_training(training_items(hosts, overview)),
        "failure_inbox": compact_failures(failure_inbox_items(hosts, overview)),
        "servers": compact_servers(hosts),
        "local_labgpu_plan": {
            "intent": local.get("intent"),
            "reply": local.get("reply"),
            "copy": local.get("copy"),
        },
        "rules": [
            "Read-only/copy-only. Do not execute commands.",
            "Only suggest LabGPU/SSH/CUDA commands that the user can copy.",
            "Secrets are redacted before context is sent.",
        ],
    }
    text = json.dumps(redact_for_assistant(context), ensure_ascii=False, indent=2)
    if len(text) > MAX_CONTEXT_CHARS:
        return text[:MAX_CONTEXT_CHARS] + "\n... truncated ..."
    return text


def compact_gpu_choices(items: object, limit: int = 8) -> list[dict[str, object]]:
    choices = ranking.filter_gpu_items(items, {"availability": "all"})
    rows: list[dict[str, object]] = []
    for item in choices[:limit]:
        rec = ranking.gpu_recommendation(item)
        rows.append(
            {
                "server": item.get("server"),
                "group": item.get("server_group"),
                "gpu": item.get("index"),
                "model": item.get("name"),
                "free_memory": ranking.format_memory(item.get("memory_free_mb")),
                "total_memory": ranking.format_memory(item.get("memory_total_mb")),
                "utilization_gpu": item.get("utilization_gpu"),
                "availability": item.get("availability") or item.get("status"),
                "recommendation": rec.get("label"),
                "reason": rec.get("reason"),
                "ssh": item.get("ssh_command"),
                "cuda_visible_devices": item.get("cuda_visible_devices"),
            }
        )
    return rows


def compact_training(items: list[dict[str, object]], limit: int = 8) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in items[:limit]:
        rows.append(
            {
                "kind": item.get("kind"),
                "name": item.get("name"),
                "host": item.get("host"),
                "group": item.get("group"),
                "gpu": item.get("gpu"),
                "pid": item.get("pid"),
                "status": item.get("status"),
                "runtime": item.get("runtime"),
                "health": item.get("health"),
                "diagnosis": item.get("diagnosis"),
                "command": redact_command(str(item.get("command") or "")),
            }
        )
    return rows


def compact_failures(items: list[dict[str, object]], limit: int = 8) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in items[:limit]:
        rows.append(
            {
                "source": item.get("source") or item.get("kind"),
                "name": item.get("name"),
                "host": item.get("host"),
                "gpu": item.get("gpu"),
                "status": item.get("status"),
                "health": item.get("health"),
                "diagnosis": item.get("diagnosis"),
            }
        )
    return rows


def compact_servers(hosts: list[object], limit: int = 12) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for host in hosts[:limit]:
        if not isinstance(host, dict):
            continue
        gpus = [gpu for gpu in host.get("gpus") or [] if isinstance(gpu, dict)]
        rows.append(
            {
                "alias": host.get("alias"),
                "group": host.get("group"),
                "online": host.get("online"),
                "mode": host.get("mode"),
                "tags": host.get("tags") or [],
                "gpu_count": len(gpus),
                "free_gpus": sum(1 for gpu in gpus if gpu.get("availability") in {"free", "probably_available"} or gpu.get("status") == "free"),
                "alerts": len(host.get("alerts") or []),
                "error": host.get("error"),
            }
        )
    return rows


def redact_for_assistant(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_for_assistant(item) for key, item in value.items() if not sensitive_key(str(key))}
    if isinstance(value, list):
        return [redact_for_assistant(item) for item in value]
    if isinstance(value, str):
        return redact_command(value)
    return value


def sensitive_key(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD"))


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
