import os
import tempfile
import unittest
from pathlib import Path

from labgpu.cli.context import build_context, render_markdown
from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore


class ContextTest(unittest.TestCase):
    def test_context_contains_diagnosis_env_git_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LABGPU_HOME"] = tmp
            store = RunStore(Path(tmp) / "runs")
            run_id = "demo-20260428-120000"
            run_dir = store.run_dir(run_id)
            log_path = run_dir / "stdout.log"
            meta = RunMeta(
                run_id=run_id,
                name="demo",
                user="alice",
                host="gpu01",
                status="failed",
                created_at="2026-04-28T12:00:00+00:00",
                command="python train.py",
                cwd="/work/demo",
                log_path=str(log_path),
                failure_reason="CUDA out of memory",
            )
            store.create(meta)
            log_path.write_text("start\nCUDA out of memory\n", encoding="utf-8")
            (run_dir / "diagnosis.json").write_text(
                '{"type":"cuda_oom","title":"CUDA out of memory","severity":"error","evidence":"line 2","line_number":2,"suggestion":"reduce batch size"}\n',
                encoding="utf-8",
            )
            (run_dir / "env.json").write_text('{"python_version":"3.11","CUDA_VISIBLE_DEVICES":"0"}\n', encoding="utf-8")
            (run_dir / "git.json").write_text('{"git_commit":"abc123","git_dirty":true}\n', encoding="utf-8")
            payload = build_context(store, meta, tail=20)
            self.assertEqual(payload["diagnosis"]["type"], "cuda_oom")
            markdown = render_markdown(payload)
            self.assertIn("# LabGPU Debug Context: demo", markdown)
            self.assertIn("CUDA out of memory", markdown)
            self.assertIn("abc123", markdown)


if __name__ == "__main__":
    unittest.main()
