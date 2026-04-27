import tempfile
import unittest
from pathlib import Path

from labgpu.cli.logs import _tail


class LogsTest(unittest.TestCase):
    def test_tail_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stdout.log"
            path.write_text("a\nb\nc\n", encoding="utf-8")
            self.assertEqual(_tail(path, 2), "b\nc\n")


if __name__ == "__main__":
    unittest.main()
