import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout

from labgpu.cli import pick
from labgpu.remote.dashboard import collect_servers, filter_gpu_items, gpu_recommendation, owner_message
from labgpu.remote.demo import fake_lab_hosts
from labgpu.remote.history import append_history, apply_history_evidence, read_history
from labgpu.remote.ranking import rank_gpus


class PickAndHistoryTest(unittest.TestCase):
    def test_fake_lab_pick_recommends_available_gpu(self):
        args = Namespace(
            config=None,
            hosts=None,
            pattern=None,
            timeout=1,
            fake_lab=True,
            model="A100",
            tag="training",
            min_free_gb=40,
            all=False,
            limit=3,
            json=True,
            explain=False,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = pick.run(args)
        self.assertEqual(code, 0)
        rows = json.loads(output.getvalue())
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "Recommended")
        self.assertEqual(rows[0]["server"], "alpha_liu")
        self.assertEqual(rows[0]["cuda_visible_devices"], "0")
        self.assertIn("reasons", rows[0])

    def test_pick_explain_prints_reasons(self):
        args = Namespace(
            config=None,
            hosts=None,
            pattern=None,
            timeout=1,
            fake_lab=True,
            model="A100",
            tag="training",
            min_vram="24G",
            min_free_gb=0,
            all=False,
            limit=1,
            json=False,
            cmd=False,
            explain=True,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = pick.run(args)
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("why:", text)
        self.assertIn("free", text)

    def test_pick_cmd_accepts_training_command(self):
        args = Namespace(
            config=None,
            hosts=None,
            pattern=None,
            timeout=1,
            fake_lab=True,
            model="A100",
            tag="training",
            min_vram="24G",
            min_free_gb=0,
            all=False,
            limit=1,
            json=False,
            cmd="python train.py --config configs/sft.yaml",
            explain=False,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = pick.run(args)
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("ssh alpha_liu", text)
        self.assertIn("CUDA_VISIBLE_DEVICES=0", text)
        self.assertIn("python train.py --config configs/sft.yaml", text)

    def test_gpu_recommendation_uses_score_and_busy_label(self):
        data = collect_servers(fake_lab=True)
        choices = filter_gpu_items(data["overview"]["gpu_items"], {"availability": "all"})
        labels = {item["availability"]: gpu_recommendation(item)["label"] for item in choices}
        self.assertIn(labels["idle_but_occupied"], {"Not recommended", "Busy"})
        self.assertEqual(labels["busy"], "Busy")
        scores = [int(gpu_recommendation(item)["score"]) for item in choices]
        self.assertTrue(all(0 <= score <= 100 for score in scores))

    def test_free_gpu_recommendation_focuses_on_gpu_availability(self):
        item = {
            "availability": "free",
            "memory_free_mb": 81920,
            "memory_total_mb": 81920,
            "name": "NVIDIA A100-SXM4-80GB",
            "disk_health": "critical",
            "server_alerts": [{"severity": "error"}],
            "load": {"ratio": 0.02},
        }
        rec = gpu_recommendation(item)
        self.assertEqual(rec["label"], "Recommended")
        self.assertGreaterEqual(int(rec["score"]), 80)
        self.assertIn("free memory", rec["reason"].lower())

    def test_rank_gpus_returns_copyable_cross_host_recommendations(self):
        ranked = rank_gpus(fake_lab_hosts(), min_vram_mb=24 * 1024, prefer="A100", tag="training")
        self.assertGreaterEqual(len(ranked), 1)
        self.assertEqual(ranked[0].host, "alpha_liu")
        self.assertEqual(ranked[0].gpu_index, "0")
        self.assertIn(ranked[0].rank, {"recommended", "ok"})
        self.assertIn("ssh alpha_liu", ranked[0].ssh_command)
        self.assertIn("CUDA_VISIBLE_DEVICES=0", ranked[0].cuda_command)

    def test_history_promotes_possible_idle_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("LABGPU_HOME")
            os.environ["LABGPU_HOME"] = tmp
            try:
                server = fake_lab_hosts()[0]
                server["alias"] = "hist"
                for _ in range(5):
                    append_history(server)
                history = read_history("hist")
                enriched = apply_history_evidence(server, history)
            finally:
                if old is None:
                    os.environ.pop("LABGPU_HOME", None)
                else:
                    os.environ["LABGPU_HOME"] = old
        idle = enriched["gpus"][1]
        self.assertEqual(idle["availability"], "idle_but_occupied")
        self.assertEqual(idle["confidence"], "high")
        self.assertIn("VRAM", owner_message(enriched["processes"][0], server_alias="hist"))

    def test_history_idle_evidence_uses_elapsed_time(self):
        server = fake_lab_hosts()[0]
        gpu = server["gpus"][1]
        rows = []
        for minute in (0, 4, 8, 12, 18):
            rows.append(
                {
                    "time": f"2026-04-28T12:{minute:02d}:00+00:00",
                    "gpus": [
                        {
                            "uuid": gpu["uuid"],
                            "utilization_gpu": 0,
                            "memory_used_mb": gpu["memory_used_mb"],
                        }
                    ],
                    "processes": [],
                }
            )
        enriched = apply_history_evidence(server, rows)
        evidence = enriched["gpus"][1]["idle_evidence"]
        self.assertEqual(evidence["elapsed_seconds"], 18 * 60)
        self.assertIn("18m00s", evidence["summary"])

    def test_history_does_not_mark_currently_free_gpu_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("LABGPU_HOME")
            os.environ["LABGPU_HOME"] = tmp
            try:
                server = fake_lab_hosts()[0]
                server["alias"] = "hist-free"
                for _ in range(5):
                    append_history(server)
                current = fake_lab_hosts()[0]
                current["alias"] = "hist-free"
                current["gpus"][1]["memory_used_mb"] = 1
                current["gpus"][1]["memory_free_mb"] = current["gpus"][1]["memory_total_mb"] - 1
                current["gpus"][1]["utilization_gpu"] = 0
                current["gpus"][1]["processes"] = []
                current["gpus"][1]["status"] = "free"
                current["gpus"][1]["availability"] = "free"
                current["gpus"][1]["health_status"] = "healthy"
                current["gpus"][1].pop("idle_evidence", None)
                enriched = apply_history_evidence(current, read_history("hist-free"))
            finally:
                if old is None:
                    os.environ.pop("LABGPU_HOME", None)
                else:
                    os.environ["LABGPU_HOME"] = old

        self.assertNotEqual(enriched["gpus"][1].get("availability"), "idle_but_occupied")
        self.assertNotIn("idle_evidence", enriched["gpus"][1])


if __name__ == "__main__":
    unittest.main()
