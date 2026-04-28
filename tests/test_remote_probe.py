import unittest

from labgpu.remote.probe import parse_probe_output, redact_command


class RemoteProbeTest(unittest.TestCase):
    def test_parse_probe_output(self):
        output = """__LABGPU_SECTION__ host
a100
__LABGPU_SECTION__ current_user
alice
__LABGPU_SECTION__ uptime
up 3 days
__LABGPU_SECTION__ load
1.00 2.00 3.00 4/100 123
__LABGPU_SECTION__ nproc
64
__LABGPU_SECTION__ memory
mem\t1000\t250\t700
swap\t2000\t100\t1900
__LABGPU_SECTION__ disks
/dev/sda1 100G 90G 10G 90% /
__LABGPU_SECTION__ gpus
0, GPU-abc, NVIDIA A100, 00000000:01:00.0, 81920, 1024, 12, 45
__LABGPU_SECTION__ processes
1234\tGPU-abc\t512\talice\t100\t100\t3600\tTue Apr 28 02:00:00 2026\tR\t12.5\t1.2\t/home/alice/work\tpython train.py --token secret
__LABGPU_SECTION__ labgpu
available=1
"""
        payload = parse_probe_output(output)
        self.assertEqual(payload["remote_hostname"], "a100")
        self.assertEqual(payload["disk"]["use_percent"], "90%")
        self.assertEqual(payload["cpu_cores"], 64)
        self.assertEqual(payload["memory"]["mem"]["used_percent"], 25)
        self.assertEqual(payload["gpus"][0]["uuid"], "GPU-abc")
        self.assertEqual(payload["gpus"][0]["processes"][0]["user"], "alice")
        self.assertEqual(payload["gpus"][0]["processes"][0]["runtime_seconds"], 3600)
        self.assertTrue(payload["gpus"][0]["processes"][0]["is_current_user"])
        self.assertIn("--token <redacted>", payload["gpus"][0]["processes"][0]["command"])
        self.assertTrue(payload["labgpu_available"])

    def test_redact_command(self):
        command = "OPENAI_API_KEY=sk-abc python train.py --password hunter2 --max_new_tokens 128 --ok value"
        redacted = redact_command(command)
        self.assertNotIn("sk-abc", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertIn("--max_new_tokens 128", redacted)
        self.assertIn("--ok value", redacted)


if __name__ == "__main__":
    unittest.main()
