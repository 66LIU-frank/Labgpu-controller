import unittest

from labgpu.gpu.nvidia_smi import parse_gpu_query, parse_process_query


class NvidiaSmiParserTest(unittest.TestCase):
    def test_parse_gpu_query(self):
        output = "0, GPU-abc, NVIDIA A100, 00000000:01:00.0, 81920, 1024, 12, 45\n"
        gpus = parse_gpu_query(output)
        self.assertEqual(gpus[0]["uuid"], "GPU-abc")
        self.assertEqual(gpus[0]["memory_total_mb"], 81920)
        self.assertEqual(gpus[0]["pci_bus_id"], "00000000:01:00.0")

    def test_parse_process_query(self):
        output = "1234, GPU-abc, 2048\n"
        processes = parse_process_query(output)
        self.assertEqual(processes[0]["pid"], 1234)
        self.assertEqual(processes[0]["used_memory_mb"], 2048)


if __name__ == "__main__":
    unittest.main()
