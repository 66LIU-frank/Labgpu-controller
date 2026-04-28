import getpass
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from labgpu.cli import adopt, run as run_cmd


class RunAutoAndAdoptTest(unittest.TestCase):
    def test_run_gpu_auto_resolves_to_local_index(self):
        with patch("labgpu.cli.run.pick_local_gpu") as pick:
            pick.return_value = {
                "index": 3,
                "name": "NVIDIA A100",
                "memory_free_mb": 80 * 1024,
            }
            output = io.StringIO()
            with redirect_stdout(output):
                gpu = run_cmd.resolve_gpu("auto", min_vram="24G", prefer="A100")
        self.assertEqual(gpu, "3")
        self.assertIn("selected GPU 3", output.getvalue())
        pick.assert_called_once_with(min_vram_mb=24 * 1024, prefer="A100")

    def test_adopt_rejects_other_owner_by_default(self):
        with self.assertRaises(RuntimeError):
            adopt.ensure_owner_allowed({"pid": 123, "user": "someone_else"})

    def test_adopt_allows_current_owner(self):
        adopt.ensure_owner_allowed({"pid": 123, "user": getpass.getuser()})

    def test_adopt_auto_detects_gpu_from_pid(self):
        with patch("labgpu.cli.adopt.detect_pid_gpus", return_value=["2"]):
            output = io.StringIO()
            with redirect_stdout(output):
                gpu = adopt.resolve_adopt_gpu(123, None)
        self.assertEqual(gpu, "2")
        self.assertIn("detected PID 123 on GPU 2", output.getvalue())


if __name__ == "__main__":
    unittest.main()
