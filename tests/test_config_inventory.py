import tempfile
import unittest
from pathlib import Path

from labgpu.core.config import LabGPUConfig, ServerEntry, parse_config, render_config, write_config
from labgpu.remote.inventory import import_ssh_hosts, load_inventory


class ConfigInventoryTest(unittest.TestCase):
    def test_parse_and_render_config(self):
        config = parse_config(
            """
[ui]
refresh_interval_seconds = 10
safe_mode = true
onboarding_completed = true

[groups]
names = ["AlphaLab", "liusuu"]

[servers.alpha_liu]
enabled = true
alias = "alpha_liu"
group = "AlphaLab"
tags = ["A100", "training"]
disk_paths = ["/", "/data"]
shared_account = true
allow_stop_own_process = false
ai_extra_paths = ["~/miniconda3/bin"]
claude_command = "~/miniconda3/bin/claude"
codex_command = "~/.local/bin/codex"
"""
        )
        server = config.servers["alpha_liu"]
        self.assertEqual(config.ui.refresh_interval_seconds, 10)
        self.assertTrue(config.ui.onboarding_completed)
        self.assertEqual(config.groups, ["AlphaLab", "liusuu"])
        self.assertEqual(server.group, "AlphaLab")
        self.assertEqual(server.tags, ["A100", "training"])
        self.assertEqual(server.ai_extra_paths, ["~/miniconda3/bin"])
        self.assertEqual(server.claude_command, "~/miniconda3/bin/claude")
        self.assertEqual(server.codex_command, "~/.local/bin/codex")
        self.assertTrue(server.shared_account)
        self.assertFalse(server.allow_stop_own_process)
        rendered = render_config(config)
        self.assertIn("onboarding_completed = true", rendered)
        self.assertIn('[groups]\nnames = ["AlphaLab", "liusuu"]', rendered)
        self.assertIn("[servers.alpha_liu]", rendered)
        self.assertIn('group = "AlphaLab"', rendered)
        self.assertIn('tags = ["A100", "training"]', rendered)
        self.assertIn('ai_extra_paths = ["~/miniconda3/bin"]', rendered)
        self.assertIn('claude_command = "~/miniconda3/bin/claude"', rendered)
        self.assertIn('codex_command = "~/.local/bin/codex"', rendered)

    def test_inventory_uses_saved_servers_when_no_hosts_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ssh_config = root / "ssh_config"
            ssh_config.write_text("Host alpha_liu\n  HostName 10.0.0.1\n", encoding="utf-8")
            lab_config = LabGPUConfig()
            lab_config.servers["alpha_liu"] = ServerEntry(
                name="alpha_liu",
                alias="alpha_liu",
                group="AlphaLab",
                tags=["A100"],
                disk_paths=["/", "/data"],
                shared_account=True,
                allow_stop_own_process=False,
                ai_extra_paths=["~/miniconda3/bin"],
                claude_command="~/miniconda3/bin/claude",
                codex_command="~/.local/bin/codex",
            )
            config_path = root / "config.toml"
            write_config(lab_config, config_path)
            hosts = load_inventory(ssh_config=ssh_config, config_path=config_path)
            self.assertEqual([host.alias for host in hosts], ["alpha_liu"])
            self.assertEqual(hosts[0].group, "AlphaLab")
            self.assertEqual(hosts[0].tags, ["A100"])
            self.assertEqual(hosts[0].disk_paths, ["/", "/data"])
            self.assertEqual(hosts[0].ai_extra_paths, ["~/miniconda3/bin"])
            self.assertEqual(hosts[0].claude_command, "~/miniconda3/bin/claude")
            self.assertEqual(hosts[0].codex_command, "~/.local/bin/codex")
            self.assertTrue(hosts[0].shared_account)

    def test_import_ssh_hosts_writes_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ssh_config = root / "ssh_config"
            ssh_config.write_text("Host alpha_liu\nHost song_1\n", encoding="utf-8")
            config_path = root / "config.toml"
            _config, imported = import_ssh_hosts(
                ssh_config=ssh_config,
                names=["alpha_liu"],
                tags=["A100"],
                group="AlphaLab",
                config_path=config_path,
            )
            self.assertEqual([entry.alias for entry in imported], ["alpha_liu"])
            self.assertEqual(imported[0].group, "AlphaLab")
            self.assertIn('names = ["AlphaLab"]', config_path.read_text(encoding="utf-8"))
            self.assertIn('group = "AlphaLab"', config_path.read_text(encoding="utf-8"))
            self.assertIn('tags = ["A100"]', config_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
