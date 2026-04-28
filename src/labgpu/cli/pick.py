from __future__ import annotations

import json

from labgpu.remote.dashboard import collect_servers, split_hosts
from labgpu.remote.ranking import filter_gpu_items, format_memory, gpu_recommendation, gpu_recommendation_reasons, launch_snippet


def run(args) -> int:
    data = collect_servers(
        ssh_config=args.config,
        names=split_hosts(args.hosts),
        pattern=args.pattern,
        timeout=args.timeout,
        fake_lab=args.fake_lab,
    )
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    ui = {
        "model": getattr(args, "prefer", None) or getattr(args, "model", None) or "",
        "prefer": getattr(args, "prefer", None) or getattr(args, "model", None) or "",
        "tag": args.tag or "",
        "min_vram": getattr(args, "min_vram", None) or (str(getattr(args, "min_free_gb", "") or "") if getattr(args, "min_free_gb", None) else ""),
        "availability": "all" if args.all else "available",
    }
    items = filter_gpu_items(overview.get("gpu_items") or [], ui)
    prefer = getattr(args, "prefer", None) or getattr(args, "model", None) or ""
    rows = [pick_row(item, prefer=prefer) for item in items[: args.limit]]
    if getattr(args, "cmd", False):
        if not rows:
            return 1
        for row in rows:
            print(row["launch_snippet"])
        return 0
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if not rows:
        busy = overview.get("busy_gpus", 0)
        idle = overview.get("suspected_idle_gpus", 0)
        critical = overview.get("critical_alerts", 0)
        print("No clearly free GPU found.")
        print(f"{busy} GPUs are busy.")
        print(f"{idle} GPUs look idle but occupied.")
        if critical:
            print(f"{critical} critical alert(s) may make servers not recommended.")
        return 1
    print(f"{'Score':<6} {'Label':<15} {'Server':<18} {'GPU':<5} {'Model':<28} {'Free':<10} {'Copy'}")
    for row in rows:
        print(
            f"{row['score']:<6} {row['label']:<15} {row['server']:<18} {row['gpu_index']:<5} "
            f"{str(row['model'])[:27]:<28} {row['free_memory']:<10} {row['ssh_command']} ; CUDA_VISIBLE_DEVICES={row['cuda_visible_devices']}"
        )
        if getattr(args, "explain", False):
            print("       why:")
            for reason in row["reasons"]:
                print(f"       - {reason}")
        print(f"       launch: {row['launch_snippet']}")
    return 0


def pick_row(item: dict[str, object], *, prefer: object = None) -> dict[str, object]:
    recommendation = gpu_recommendation(item, prefer=prefer)
    return {
        "score": recommendation["score"],
        "label": recommendation["label"],
        "reason": recommendation["reason"],
        "reasons": gpu_recommendation_reasons(item, recommendation, prefer=prefer),
        "server": item.get("server"),
        "gpu_index": item.get("index"),
        "model": item.get("name"),
        "free_memory": format_memory(item.get("memory_free_mb")),
        "free_memory_mb": item.get("memory_free_mb"),
        "ssh_command": item.get("ssh_command") or f"ssh {item.get('server')}",
        "cuda_visible_devices": item.get("cuda_visible_devices") or str(item.get("index")),
        "launch_snippet": launch_snippet(item),
        "disk_health": item.get("disk_health"),
        "availability": item.get("availability"),
    }
