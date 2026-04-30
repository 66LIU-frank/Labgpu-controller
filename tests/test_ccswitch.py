import sqlite3
import tempfile
import unittest
from pathlib import Path

from labgpu.remote.ccswitch import read_ccswitch_summary


class CcSwitchTest(unittest.TestCase):
    def test_reads_non_secret_provider_and_proxy_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / ".cc-switch"
            db_dir.mkdir()
            db_path = db_dir / "cc-switch.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, settings_config TEXT, is_current BOOLEAN DEFAULT 0, PRIMARY KEY(id, app_type))"
            )
            conn.execute(
                "CREATE TABLE proxy_config (app_type TEXT PRIMARY KEY, listen_address TEXT, listen_port INTEGER, proxy_enabled BOOLEAN, enabled BOOLEAN)"
            )
            conn.execute("INSERT INTO providers VALUES ('codex-pro', 'codex', 'pro', 'SECRET', 1)")
            conn.execute("INSERT INTO providers VALUES ('codex-alt', 'codex', 'alt', 'SECRET', 0)")
            conn.execute("INSERT INTO providers VALUES ('claude-main', 'claude', 'main', 'SECRET', 1)")
            conn.execute("INSERT INTO proxy_config VALUES ('codex', '127.0.0.1', 15721, 1, 1)")
            conn.commit()
            conn.close()

            summary = read_ccswitch_summary(tmp)

        self.assertTrue(summary["available"])
        self.assertEqual(summary["providers"]["codex"]["current"], "pro")
        self.assertEqual(summary["providers"]["claude"]["current"], "main")
        self.assertEqual(summary["proxy"]["codex"]["listen_port"], 15721)
        self.assertNotIn("SECRET", str(summary))


if __name__ == "__main__":
    unittest.main()
