import os
import tempfile
import unittest
from pathlib import Path

from labgpu.cli.status import collect_status
from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore


class StatusTrackingModesTest(unittest.TestCase):
    def test_fake_status_marks_adopted_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LABGPU_HOME"] = tmp
            store = RunStore(Path(tmp) / "runs")
            store.create(
                RunMeta(
                    run_id="adopted-20260428-120000",
                    name="adopted",
                    user=os.environ.get("USER", "user"),
                    host="localhost",
                    status="running",
                    created_at="2026-04-28T12:00:00+00:00",
                    pid=os.getpid(),
                    launch_mode="adopted",
                )
            )
            payload = collect_status(fake=True)
            proc = payload["gpu"]["gpus"][0]["processes"][0]
            self.assertEqual(proc["tracking_status"], "adopted")
            self.assertEqual(proc["matched_run_name"], "adopted")


if __name__ == "__main__":
    unittest.main()
