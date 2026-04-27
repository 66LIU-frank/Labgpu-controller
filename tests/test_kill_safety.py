import os
import tempfile
import unittest
from pathlib import Path

from labgpu.cli.resolve import resolve_run_strict
from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore


class KillSafetyTest(unittest.TestCase):
    def test_strict_resolve_rejects_multiple_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LABGPU_HOME"] = tmp
            store = RunStore(Path(tmp) / "runs")
            for run_id in ["dup-20260428-120000", "dup-20260428-130000"]:
                store.create(
                    RunMeta(
                        run_id=run_id,
                        name="dup",
                        user="alice",
                        host="gpu01",
                        status="running",
                        created_at="2026-04-28T12:00:00+00:00",
                    )
                )
            with self.assertRaisesRegex(RuntimeError, "multiple runs matched"):
                resolve_run_strict(store, "dup", action="kill")


if __name__ == "__main__":
    unittest.main()
