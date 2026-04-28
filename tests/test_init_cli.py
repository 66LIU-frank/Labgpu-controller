import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from labgpu.cli import init as init_cmd
from labgpu.core.config import LabGPUConfig, ServerEntry, load_config, write_config


class InitCliTest(unittest.TestCase):
    def test_init_saves_selected_hosts_and_disables_unselected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ssh_config = root / "ssh_config"
            ssh_config.write_text(
                """
Host alpha_liu
  HostName alpha.example

Host alpha_shi
  HostName shi.example
""",
                encoding="utf-8",
            )
            config_path = root / "config.toml"
            existing = LabGPUConfig()
            existing.servers["alpha_shi"] = ServerEntry(name="alpha_shi", alias="alpha_shi", enabled=True)
            write_config(existing, config_path)

            args = Namespace(
                hosts="alpha_liu",
                pattern=None,
                config=str(ssh_config),
                labgpu_config=str(config_path),
                tags="A100,training",
                shared_account=False,
                timeout=1,
                no_probe=True,
                keep_existing=False,
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(init_cmd.run(args), 0)
            saved = load_config(config_path)

        self.assertTrue(saved.servers["alpha_liu"].enabled)
        self.assertEqual(saved.servers["alpha_liu"].tags, ["A100", "training"])
        self.assertFalse(saved.servers["alpha_liu"].shared_account)
        self.assertFalse(saved.servers["alpha_shi"].enabled)

    def test_detect_model_tags_from_probe_payload(self):
        payload = {
            "gpus": [
                {"name": "NVIDIA A100-SXM4-80GB"},
                {"name": "NVIDIA GeForce RTX 4090"},
            ]
        }
        self.assertEqual(init_cmd.detect_model_tags(payload), ["A100", "4090"])


if __name__ == "__main__":
    unittest.main()
