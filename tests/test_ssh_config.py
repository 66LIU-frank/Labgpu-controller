import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labgpu.remote.ssh_config import SSHHost, parse_ssh_config, resolve_ssh_host, select_hosts


class SSHConfigTest(unittest.TestCase):
    def test_parse_concrete_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config"
            path.write_text(
                """
Host *
  ServerAliveInterval 60

Host alpha_liu
  HostName 210.45.70.34
  User lsg
  Port 1722

Host Song-1 Song-2
  HostName 210.45.70.34
  User shiyr
""",
                encoding="utf-8",
            )
            hosts = parse_ssh_config(path)
            aliases = [host.alias for host in hosts]
            self.assertEqual(aliases, ["alpha_liu", "Song-1", "Song-2"])
            self.assertEqual(hosts[0].user, "lsg")
            self.assertEqual(hosts[0].port, "1722")

    def test_select_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config"
            path.write_text("Host alpha_liu\nHost Song-1\n", encoding="utf-8")
            hosts = parse_ssh_config(path)
            self.assertEqual([host.alias for host in select_hosts(hosts, pattern="song")], ["Song-1"])
            self.assertEqual([host.alias for host in select_hosts(hosts, names=["alpha_liu"])], ["alpha_liu"])
            self.assertEqual([host.alias for host in select_hosts(hosts, names=["missing_alias"])], ["missing_alias"])

    def test_include_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            include = root / "included"
            include.write_text("Host beta\n  HostName beta.local\n", encoding="utf-8")
            config = root / "config"
            config.write_text(f"Include {include}\nHost alpha\n", encoding="utf-8")
            hosts = parse_ssh_config(config)
            self.assertEqual([host.alias for host in hosts], ["beta", "alpha"])

    def test_resolve_ssh_host_uses_ssh_g(self):
        output = """hostname 10.0.0.1
user alice
port 2222
proxyjump bastion
identityfile ~/.ssh/id_ed25519
"""

        class Result:
            returncode = 0
            stdout = output

        with patch("labgpu.remote.ssh_config.subprocess.run", return_value=Result()) as run:
            host = resolve_ssh_host(SSHHost(alias="alpha"))
        run.assert_called_once()
        self.assertEqual(host.hostname, "10.0.0.1")
        self.assertEqual(host.user, "alice")
        self.assertEqual(host.port, "2222")
        self.assertEqual(host.proxyjump, "bastion")
        self.assertEqual(host.identity_files, ["~/.ssh/id_ed25519"])


if __name__ == "__main__":
    unittest.main()
