import unittest

from labgpu.diagnose.scanner import scan_text


class DiagnoseTest(unittest.TestCase):
    def test_cuda_oom(self):
        result = scan_text("torch.cuda.OutOfMemoryError: CUDA out of memory")
        self.assertEqual(result["type"], "cuda_oom")

    def test_module_not_found(self):
        result = scan_text("ModuleNotFoundError: No module named 'torch'")
        self.assertEqual(result["type"], "module_not_found")


if __name__ == "__main__":
    unittest.main()
