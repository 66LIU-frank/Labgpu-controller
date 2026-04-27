import os
import tempfile
import unittest
from pathlib import Path

from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore


class StoreTest(unittest.TestCase):
    def test_create_list_resolve_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LABGPU_HOME"] = tmp
            store = RunStore(Path(tmp) / "runs")
            meta = RunMeta(
                run_id="baseline-20260427-120000",
                name="baseline",
                user="alice",
                host="gpu01",
                status="created",
                created_at="2026-04-27T12:00:00+00:00",
            )
            store.create(meta)
            self.assertEqual(store.resolve("baseline").run_id, meta.run_id)
            updated = store.update(meta.run_id, status="running")
            self.assertEqual(updated.status, "running")
            self.assertTrue((Path(tmp) / "runs" / meta.run_id / "events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
