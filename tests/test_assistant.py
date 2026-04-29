import unittest
from unittest.mock import patch

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

    def test_assistant_can_use_byo_openai_compatible_api(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"API answer"}}]}'

        options = {
            "mode": "api",
            "api_url": "https://example.test/v1",
            "model": "test-model",
            "api_key": "sk-test",
        }
        with patch("labgpu.remote.assistant.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            reply = assistant_reply(fake_lab_data(), "Find me an A100", options=options)

        self.assertTrue(reply["ok"])
        self.assertEqual(reply["mode"], "api")
        self.assertEqual(reply["reply"], "API answer")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
        self.assertIn("Bearer sk-test", request.headers.get("Authorization", ""))

    def test_assistant_api_falls_back_to_local_without_model(self):
        reply = assistant_reply(fake_lab_data(), "Find me an A100", options={"mode": "api", "api_url": "https://example.test/v1"})
        self.assertEqual(reply["mode"], "local")
        self.assertIn("Falling back", reply["reply"])


if __name__ == "__main__":
    unittest.main()
