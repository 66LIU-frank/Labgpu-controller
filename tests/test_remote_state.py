import unittest

from labgpu.remote.state import annotate_server, build_overview, human_duration


class RemoteStateTest(unittest.TestCase):
    def test_available_gpus_my_processes_and_alerts(self):
        server = annotate_server(
            {
                "alias": "alpha",
                "online": True,
                "disks": [{"mount": "/", "use_percent": "96%"}],
                "gpus": [
                    {
                        "index": 0,
                        "uuid": "GPU-free",
                        "name": "A100",
                        "memory_total_mb": 81920,
                        "memory_used_mb": 0,
                        "memory_free_mb": 81920,
                        "utilization_gpu": 0,
                        "processes": [],
                    },
                    {
                        "index": 1,
                        "uuid": "GPU-busy",
                        "name": "A100",
                        "memory_total_mb": 81920,
                        "memory_used_mb": 60000,
                        "memory_free_mb": 21920,
                        "utilization_gpu": 0,
                        "processes": [
                            {
                                "pid": 123,
                                "gpu_uuid": "GPU-busy",
                                "user": "alice",
                                "is_current_user": True,
                                "runtime_seconds": 3600,
                                "used_memory_mb": 60000,
                                "state": "R",
                            }
                        ],
                    },
                ],
                "processes": [
                    {
                        "pid": 123,
                        "gpu_uuid": "GPU-busy",
                        "user": "alice",
                        "is_current_user": True,
                        "runtime_seconds": 3600,
                        "used_memory_mb": 60000,
                        "state": "R",
                    }
                ],
            }
        )
        self.assertEqual(server["available_gpus"][0]["gpu_index"], 0)
        self.assertEqual(server["my_processes"][0]["runtime"], "1h00m")
        self.assertGreaterEqual(len(server["alerts"]), 1)
        overview = build_overview([server])
        self.assertEqual(overview["available_gpus"], 1)
        self.assertEqual(overview["my_processes"], 1)

    def test_human_duration(self):
        self.assertEqual(human_duration(59), "59s")
        self.assertEqual(human_duration(3600), "1h00m")
        self.assertEqual(human_duration(90000), "1d01h")


if __name__ == "__main__":
    unittest.main()
