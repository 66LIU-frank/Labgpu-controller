import unittest

from labgpu.remote.assistant import assistant_reply
from labgpu.remote.demo import fake_lab_data


class AssistantTest(unittest.TestCase):
    def test_assistant_recommends_gpu_and_copyable_plan(self):
        reply = assistant_reply(fake_lab_data(), "Find me a 24G A100 for python train.py --config configs/sft.yaml")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["intent"], "rank_gpus")
        self.assertIn("alpha_liu", reply["reply"])
        self.assertIn("CUDA_VISIBLE_DEVICES", reply["copy"])
        self.assertIn("python train.py --config configs/sft.yaml", reply["copy"])

    def test_assistant_where_lists_training(self):
        reply = assistant_reply(fake_lab_data(), "Where are my training jobs?")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["intent"], "where")
        self.assertIn("training", reply["reply"].lower())

    def test_assistant_failures_lists_inbox(self):
        reply = assistant_reply(fake_lab_data(), "What failed?")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["intent"], "failures")
        self.assertIn("suspicious", reply["reply"].lower())


if __name__ == "__main__":
    unittest.main()
