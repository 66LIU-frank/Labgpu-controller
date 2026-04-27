import os
import unittest

from labgpu.process.inspector import inspect_process, pid_exists


class ProcessInspectorTest(unittest.TestCase):
    def test_current_process_is_inspectable(self):
        pid = os.getpid()
        self.assertTrue(pid_exists(pid))
        info = inspect_process(pid)
        self.assertEqual(info["pid"], pid)
        self.assertIn("permission_error", info)


if __name__ == "__main__":
    unittest.main()
