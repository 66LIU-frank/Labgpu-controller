import unittest

from labgpu.gpu.fake import FakeCollector


class FakeCollectorTest(unittest.TestCase):
    def test_fake_collector_has_identity_and_process(self):
        payload = FakeCollector().collect()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["gpus"][0]["uuid"], "GPU-fake-0000")
        self.assertIn("pci_bus_id", payload["gpus"][0])
        self.assertIn("cmdline", payload["gpus"][0]["processes"][0])


if __name__ == "__main__":
    unittest.main()
