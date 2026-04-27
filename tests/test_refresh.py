import os
import tempfile
import unittest
from pathlib import Path

from labgpu.core.models import RunMeta
from labgpu.core.refresh import refresh_runs
from labgpu.core.store import RunStore


class RefreshTest(unittest.TestCase):
    def test_missing_running_pid_becomes_orphaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LABGPU_HOME"] = tmp
            store = RunStore(Path(tmp) / "runs")
            store.create(
                RunMeta(
                    run_id="stale-20260428-120000",
                    name="stale",
                    user="alice",
                    host="gpu01",
                    status="running",
                    created_at="2026-04-28T12:00:00+00:00",
                    started_at="2026-04-28T12:00:00+00:00",
                    pid=99999999,
                )
            )
            result = refresh_runs(store)
            self.assertEqual(result.updated, 1)
            self.assertEqual(store.get("stale-20260428-120000").status, "orphaned")


if __name__ == "__main__":
    unittest.main()
