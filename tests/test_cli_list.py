import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from labgpu.cli.main import main
from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore


class CliListTest(unittest.TestCase):
    def test_list_prints_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LABGPU_HOME"] = tmp
            store = RunStore(Path(tmp) / "runs")
            store.create(
                RunMeta(
                    run_id="demo-20260427-120000",
                    name="demo",
                    user="alice",
                    host="gpu01",
                    status="success",
                    created_at="2026-04-27T12:00:00+00:00",
                    started_at="2026-04-27T12:00:00+00:00",
                    ended_at="2026-04-27T12:00:01+00:00",
                )
            )
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["list", "--all"])
            self.assertEqual(code, 0)
            self.assertIn("Run ID", output.getvalue())
            self.assertIn("demo-20260427-120000", output.getvalue())


if __name__ == "__main__":
    unittest.main()
