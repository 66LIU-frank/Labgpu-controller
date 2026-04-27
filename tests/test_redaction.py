import unittest

from labgpu.cli.context import prepare_env


class RedactionTest(unittest.TestCase):
    def test_sensitive_env_is_redacted(self):
        env = {
            "python_version": "3.11",
            "OPENAI_API_KEY": "sk-secret",
            "WANDB_API_KEY": "wandb-secret",
            "CUDA_VISIBLE_DEVICES": "0",
        }
        safe, redacted = prepare_env(env, include_env=True, redact=True)
        self.assertEqual(safe["OPENAI_API_KEY"], "[REDACTED]")
        self.assertEqual(safe["WANDB_API_KEY"], "[REDACTED]")
        self.assertIn("OPENAI_API_KEY", redacted)
        self.assertEqual(safe["CUDA_VISIBLE_DEVICES"], "0")

    def test_default_env_is_safe_subset(self):
        env = {"PATH": "/secret/path", "python_version": "3.11", "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}
        safe, _ = prepare_env(env, include_env=False, redact=True)
        self.assertIn("python_version", safe)
        self.assertNotIn("PATH", safe)


if __name__ == "__main__":
    unittest.main()
