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

[servers.alpha_liu]
enabled = true
alias = "alpha_liu"
tags = ["A100", "training"]
disk_paths = ["/", "/data"]
shared_account = true
allow_stop_own_process = false
"""
        )
        server = config.servers["alpha_liu"]
        self.assertEqual(config.ui.refresh_interval_seconds, 10)
        self.assertEqual(server.tags, ["A100", "training"])
        self.assertTrue(server.shared_account)
        self.assertFalse(server.allow_stop_own_process)
        rendered = render_config(config)
        self.assertIn("[servers.alpha_liu]", rendered)
        self.assertIn('tags = ["A100", "training"]', rendered)

    def test_inventory_uses_saved_servers_when_no_hosts_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ssh_config = root / "ssh_config"
            ssh_config.write_text("Host alpha_liu\n  HostName 10.0.0.1\n", encoding="utf-8")
            lab_config = LabGPUConfig()
            lab_config.servers["alpha_liu"] = ServerEntry(
                name="alpha_liu",
                alias="alpha_liu",
                tags=["A100"],
                disk_paths=["/", "/data"],
                shared_account=True,
                allow_stop_own_process=False,
            )
            config_path = root / "config.toml"
            write_config(lab_config, config_path)
            hosts = load_inventory(ssh_config=ssh_config, config_path=config_path)
            self.assertEqual([host.alias for host in hosts], ["alpha_liu"])
            self.assertEqual(hosts[0].tags, ["A100"])
            self.assertEqual(hosts[0].disk_paths, ["/", "/data"])
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
                config_path=config_path,
            )
            self.assertEqual([entry.alias for entry in imported], ["alpha_liu"])
            self.assertIn('tags = ["A100"]', config_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
