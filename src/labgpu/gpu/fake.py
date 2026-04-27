from __future__ import annotations

import os
import platform
import time
from typing import Any

from labgpu.gpu.collector import GPUCollector


class FakeCollector(GPUCollector):
    def collect(self) -> dict[str, Any]:
        pid = os.getpid()
        user = os.environ.get("USER") or "student"
        command = "python train.py --config configs/base.yaml"
        return {
            "available": True,
            "source": "fake",
            "host": platform.node() or "localhost",
            "error": None,
            "gpus": [
                {
                    "index": 0,
                    "uuid": "GPU-fake-0000",
                    "pci_bus_id": "00000000:FA:00.0",
                    "name": "Fake RTX 4090",
                    "memory_total_mb": 24564,
                    "memory_used_mb": 18420,
                    "utilization_gpu": 82,
                    "temperature": 62,
                    "processes": [
                        {
                            "pid": pid,
                            "used_memory_mb": 18420,
                            "username": user,
                            "user": user,
                            "cmdline": command,
                            "command": command,
                            "cwd": os.getcwd(),
                            "create_time": time.time(),
                            "permission_error": False,
                        }
                    ],
                },
                {
                    "index": 1,
                    "uuid": "GPU-fake-0001",
                    "pci_bus_id": "00000000:FB:00.0",
                    "name": "Fake RTX 4090",
                    "memory_total_mb": 24564,
                    "memory_used_mb": 0,
                    "utilization_gpu": 0,
                    "temperature": 35,
                    "processes": [],
                },
            ],
            "processes": [
                {
                    "pid": pid,
                    "gpu_uuid": "GPU-fake-0000",
                    "used_memory_mb": 18420,
                    "username": user,
                    "user": user,
                    "cmdline": command,
                    "command": command,
                    "cwd": os.getcwd(),
                    "create_time": time.time(),
                    "permission_error": False,
                }
            ],
        }
