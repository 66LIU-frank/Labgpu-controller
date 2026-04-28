from __future__ import annotations

from typing import Any

from labgpu.remote.state import alerts_for_server, annotate_server, build_overview
from labgpu.utils.time import now_utc


def fake_lab_hosts() -> list[dict[str, Any]]:
    probed_at = now_utc()
    hosts = [
        {
            "alias": "alpha_liu",
            "hostname": "192.168.1.17",
            "remote_hostname": "a100",
            "user": "lsg",
            "port": 22,
            "tags": ["A100", "training"],
            "disk_paths": ["/", "/data"],
            "shared_account": False,
            "allow_stop_own_process": True,
            "online": True,
            "mode": "agentless",
            "elapsed_ms": 720,
            "probed_at": probed_at,
            "uptime": "up 37 days",
            "load_avg": {"1m": 8.9, "5m": 7.4, "15m": 6.8, "ratio": 0.07},
            "cpu_cores": 128,
            "memory": {"mem": {"used_percent": 7}, "swap": {"used_percent": 21}},
            "disks": [{"mount": "/", "use_percent": "93%", "size": "1.7T", "used": "1.6T", "available": "110G"}],
            "gpus": [
                gpu(0, "GPU-alpha-0", "NVIDIA A100-SXM4-80GB", 81920, 0, 0, 31, []),
                gpu(
                    1,
                    "GPU-alpha-1",
                    "NVIDIA A100-SXM4-80GB",
                    81920,
                    60000,
                    0,
                    45,
                    [
                        proc(123, "GPU-alpha-1", "lsg", 60000, 3600 * 11, "S", 0.2, "python sft.py --config configs/a.yaml"),
                    ],
                ),
                gpu(
                    2,
                    "GPU-alpha-2",
                    "NVIDIA A100-SXM4-80GB",
                    81920,
                    70000,
                    88,
                    61,
                    [proc(456, "GPU-alpha-2", "zhangsan", 70000, 5400, "R", 88.0, "python train.py")],
                ),
            ],
            "processes": [
                proc(123, "GPU-alpha-1", "lsg", 60000, 3600 * 11, "S", 0.2, "python sft.py --config configs/a.yaml"),
                proc(456, "GPU-alpha-2", "zhangsan", 70000, 5400, "R", 88.0, "python train.py"),
            ],
        },
        {
            "alias": "song_1",
            "hostname": "10.0.0.8",
            "remote_hostname": "song-4090",
            "user": "lsg",
            "port": 22,
            "tags": ["4090", "debug"],
            "disk_paths": ["/", "/data"],
            "shared_account": False,
            "allow_stop_own_process": True,
            "online": True,
            "mode": "enhanced",
            "elapsed_ms": 310,
            "probed_at": probed_at,
            "uptime": "up 8 days",
            "load_avg": {"1m": 2.1, "5m": 2.4, "15m": 2.6, "ratio": 0.13},
            "cpu_cores": 16,
            "memory": {"mem": {"used_percent": 45}, "swap": {"used_percent": 0}},
            "disks": [{"mount": "/", "use_percent": "64%", "size": "900G", "used": "575G", "available": "325G"}],
            "labgpu_available": True,
            "labgpu_runs": [
                {"name": "bert_baseline", "status": "running", "user": "lsg", "requested_gpu_indices": [0], "failure_reason": None},
                {"name": "oom_test", "status": "failed", "user": "lsg", "requested_gpu_indices": [1], "failure_reason": "CUDA OOM"},
            ],
            "gpus": [
                gpu(0, "GPU-song-0", "NVIDIA GeForce RTX 4090", 24564, 200, 0, 35, []),
                gpu(1, "GPU-song-1", "NVIDIA GeForce RTX 4090", 24564, 18000, 74, 58, [proc(789, "GPU-song-1", "lsg", 18000, 720, "R", 72.0, "python infer.py")]),
            ],
            "processes": [proc(789, "GPU-song-1", "lsg", 18000, 720, "R", 72.0, "python infer.py")],
        },
        {
            "alias": "gpu_old",
            "hostname": "10.0.0.9",
            "user": "lsg",
            "port": 22,
            "tags": ["V100"],
            "online": False,
            "mode": "offline",
            "error": "ssh probe timed out",
            "elapsed_ms": 8000,
            "probed_at": probed_at,
        },
    ]
    annotated = [annotate_server(host) for host in hosts]
    alpha = annotated[0]
    idle_gpu = alpha["gpus"][1]
    evidence = {
        "confidence": "high",
        "low_util_samples": 6,
        "occupied_samples": 6,
        "vram_occupied_mb": 60000,
        "minutes": 10,
        "summary": "GPU util < 3% for 10+ minutes while 58.6 GB VRAM is occupied.",
    }
    idle_gpu.update(
        {
            "status": "possible_idle",
            "availability": "idle_but_occupied",
            "health_status": "suspected_idle",
            "health_severity": "warning",
            "confidence": "high",
            "idle_evidence": evidence,
            "health_reason": evidence["summary"],
        }
    )
    for proc_item in alpha["processes"]:
        if proc_item["pid"] == 123:
            proc_item.update({"idle_evidence": evidence, "confidence": "high", "health_reason": evidence["summary"], "cpu_low_samples": 6})
    for proc_item in idle_gpu["processes"]:
        proc_item.update({"idle_evidence": evidence, "confidence": "high", "health_reason": evidence["summary"], "cpu_low_samples": 6})
    alpha["my_processes"] = [dict(proc_item, server=alpha.get("alias"), remote_hostname=alpha.get("remote_hostname")) for proc_item in alpha["processes"] if proc_item.get("is_current_user")]
    alpha["alerts"] = alerts_for_server(alpha)
    return annotated


def fake_lab_data() -> dict[str, Any]:
    hosts = fake_lab_hosts()
    return {"hosts": hosts, "count": len(hosts), "overview": build_overview(hosts), "error": None, "fake_lab": True}


def gpu(index: int, uuid: str, name: str, total: int, used: int, util: int, temp: int, processes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "index": index,
        "uuid": uuid,
        "name": name,
        "pci_bus_id": f"0000:{index:02x}:00.0",
        "memory_total_mb": total,
        "memory_used_mb": used,
        "memory_free_mb": total - used,
        "utilization_gpu": util,
        "temperature": temp,
        "processes": processes,
    }


def proc(pid: int, uuid: str, user: str, memory: int, runtime: int, state: str, cpu: float, command: str) -> dict[str, Any]:
    return {
        "pid": pid,
        "gpu_uuid": uuid,
        "used_memory_mb": memory,
        "user": user,
        "runtime_seconds": runtime,
        "runtime": "",
        "state": state,
        "cpu_percent": cpu,
        "memory_percent": 1.2,
        "start_time": "Tue Apr 28 01:00:00 2026",
        "command": command,
        "command_hash": f"h{pid}",
        "is_current_user": user == "lsg",
    }
