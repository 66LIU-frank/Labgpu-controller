import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labgpu.remote.ccswitch import read_ccswitch_summary, sqlite_truthy, switch_ccswitch_provider


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

            with patch("labgpu.remote.ccswitch.is_local_proxy_listening", return_value=True):
                summary = read_ccswitch_summary(tmp)

        self.assertTrue(summary["available"])
        self.assertEqual(summary["providers"]["codex"]["current"], "pro")
        self.assertEqual(summary["providers"]["codex"]["current_id"], "codex-pro")
        self.assertEqual(summary["providers"]["codex"]["choices_detail"][0]["id"], "codex-pro")
        self.assertEqual(summary["providers"]["claude"]["current"], "main")
        self.assertEqual(summary["proxy"]["codex"]["listen_port"], 15721)
        self.assertTrue(summary["proxy"]["codex"]["listening"])
        self.assertNotIn("SECRET", str(summary))

    def test_switches_current_provider_without_reading_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / ".cc-switch"
            db_dir.mkdir()
            db_path = db_dir / "cc-switch.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, settings_config TEXT, is_current BOOLEAN DEFAULT 0, PRIMARY KEY(id, app_type))"
            )
            conn.execute("INSERT INTO providers VALUES ('codex-pro', 'codex', 'pro', 'SECRET', 1)")
            conn.execute("INSERT INTO providers VALUES ('codex-alt', 'codex', 'alt', 'SECRET', 0)")
            conn.commit()
            conn.close()

            switched = switch_ccswitch_provider("codex", "codex-alt", tmp)
            summary = read_ccswitch_summary(tmp)

        self.assertEqual(switched["provider"], "alt")
        self.assertTrue(switched["changed"])
        self.assertTrue(switched["verified"])
        self.assertEqual(switched["method"], "ccswitch_local_db_state")
        self.assertFalse(switched["secret_access"])
        self.assertIn("does not read", switched["warning"])
        self.assertEqual(summary["providers"]["codex"]["current"], "alt")
        self.assertEqual(summary["providers"]["codex"]["current_id"], "codex-alt")
        self.assertNotIn("SECRET", str(summary))
        self.assertNotIn("SECRET", str(switched))

    def test_switching_current_provider_is_verified_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / ".cc-switch"
            db_dir.mkdir()
            db_path = db_dir / "cc-switch.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, settings_config TEXT, is_current BOOLEAN DEFAULT 0, PRIMARY KEY(id, app_type))"
            )
            conn.execute("INSERT INTO providers VALUES ('claude-main', 'claude', 'main', 'SECRET', 1)")
            conn.commit()
            conn.close()

            switched = switch_ccswitch_provider("claude", "claude-main", tmp)

        self.assertFalse(switched["changed"])
        self.assertTrue(switched["verified"])
        self.assertIn("already the current", switched["message"])

    def test_sqlite_truthy_handles_text_boolean_values(self):
        self.assertTrue(sqlite_truthy("1"))
        self.assertTrue(sqlite_truthy("true"))
        self.assertTrue(sqlite_truthy(1))
        self.assertFalse(sqlite_truthy("0"))
        self.assertFalse(sqlite_truthy("false"))
        self.assertFalse(sqlite_truthy(0))


if __name__ == "__main__":
    unittest.main()
