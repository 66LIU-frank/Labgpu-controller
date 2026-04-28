import os
import unittest

from labgpu.gpu.select import detect_pid_gpus, pick_local_gpu


class GpuSelectTest(unittest.TestCase):
    def test_pick_local_gpu_prefers_free_gpu(self):
        payload = {
            "available": True,
            "gpus": [
                {"index": 0, "uuid": "GPU-0", "name": "A100", "memory_total_mb": 81920, "memory_used_mb": 12000, "utilization_gpu": 90, "processes": [{"pid": 123}]},
                {"index": 1, "uuid": "GPU-1", "name": "A100", "memory_total_mb": 81920, "memory_used_mb": 0, "utilization_gpu": 0, "processes": []},
            ],
            "processes": [{"pid": 123, "gpu_uuid": "GPU-0"}],
        }
        gpu = pick_local_gpu(min_vram_mb=24 * 1024, payload=payload)
        self.assertEqual(gpu["index"], 1)

    def test_detect_pid_gpus_uses_compute_apps(self):
        pid = os.getpid()
        payload = {
            "available": True,
            "gpus": [
                {"index": 0, "uuid": "GPU-0", "processes": []},
                {"index": 2, "uuid": "GPU-2", "processes": [{"pid": pid}]},
            ],
            "processes": [{"pid": pid, "gpu_uuid": "GPU-2"}],
        }
        self.assertEqual(detect_pid_gpus(pid, payload=payload), ["2"])


if __name__ == "__main__":
    unittest.main()
