import os
import tempfile
import unittest
from pathlib import Path

from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore
from labgpu.web.app import run_diagnosis, run_json


class WebApiTest(unittest.TestCase):
    def test_run_json_and_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LABGPU_HOME"] = tmp
            store = RunStore(Path(tmp) / "runs")
            store.create(
                RunMeta(
                    run_id="demo-20260427-120000",
                    name="demo",
                    user="alice",
                    host="gpu01",
                    status="failed",
                    created_at="2026-04-27T12:00:00+00:00",
                )
            )
            diagnosis = {"type": "cuda_oom", "title": "CUDA out of memory", "severity": "error", "evidence": "line 1", "line_number": 1, "suggestion": "reduce batch size"}
            (store.run_dir("demo-20260427-120000") / "diagnosis.json").write_text(str(diagnosis).replace("'", '"'), encoding="utf-8")
            self.assertEqual(run_json("demo")["name"], "demo")
            self.assertEqual(run_diagnosis("demo")["type"], "cuda_oom")


if __name__ == "__main__":
    unittest.main()
