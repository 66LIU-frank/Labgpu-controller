import tempfile
import unittest
from pathlib import Path

from labgpu.cli.logs import _tail


class LargeLogTailTest(unittest.TestCase):
    def test_tail_does_not_need_full_file_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.log"
            path.write_text("\n".join(f"line {idx}" for idx in range(5000)) + "\n", encoding="utf-8")
            text = _tail(path, 3)
            self.assertEqual(text, "line 4997\nline 4998\nline 4999\n")


if __name__ == "__main__":
    unittest.main()
