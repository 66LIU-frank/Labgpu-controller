import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from labgpu.cli.main import main


class CliStatusTest(unittest.TestCase):
    def test_status_fake_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LABGPU_HOME"] = tmp
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["status", "--fake", "--json"])
            self.assertEqual(code, 0)
            self.assertIn("GPU-fake-0000", output.getvalue())


if __name__ == "__main__":
    unittest.main()
