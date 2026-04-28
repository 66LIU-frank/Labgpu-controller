import unittest

from labgpu.remote.probe import parse_probe_output


class RemoteProbeTest(unittest.TestCase):
    def test_parse_probe_output(self):
        output = """__LABGPU_SECTION__ host
a100
__LABGPU_SECTION__ uptime
up 3 days
__LABGPU_SECTION__ load
1.00 2.00 3.00 4/100 123
__LABGPU_SECTION__ disk
/dev/sda1 100G 90G 10G 90% /
__LABGPU_SECTION__ gpus
0, GPU-abc, NVIDIA A100, 00000000:01:00.0, 81920, 1024, 12, 45
__LABGPU_SECTION__ processes
1234\tGPU-abc\t512\talice\tpython train.py
"""
        payload = parse_probe_output(output)
        self.assertEqual(payload["remote_hostname"], "a100")
        self.assertEqual(payload["disk"]["use_percent"], "90%")
        self.assertEqual(payload["gpus"][0]["uuid"], "GPU-abc")
        self.assertEqual(payload["gpus"][0]["processes"][0]["user"], "alice")


if __name__ == "__main__":
    unittest.main()
