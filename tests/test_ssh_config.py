import tempfile
import unittest
from pathlib import Path

from labgpu.remote.ssh_config import parse_ssh_config, select_hosts


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


if __name__ == "__main__":
    unittest.main()
