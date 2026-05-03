import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labgpu.remote.ccswitch import read_ccswitch_summary, read_codex_provider_runtime, sqlite_truthy, switch_ccswitch_provider


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
            (db_dir / "settings.json").write_text(json.dumps({"currentProviderCodex": "codex-alt"}), encoding="utf-8")

            with patch("labgpu.remote.ccswitch.is_local_proxy_listening", return_value=True):
                summary = read_ccswitch_summary(tmp)

        self.assertTrue(summary["available"])
        self.assertEqual(summary["providers"]["codex"]["current"], "alt")
        self.assertEqual(summary["providers"]["codex"]["current_id"], "codex-alt")
        self.assertEqual(summary["providers"]["codex"]["current_source"], "settings")
        self.assertEqual(summary["providers"]["codex"]["db_current"], "pro")
        current_choices = [item["id"] for item in summary["providers"]["codex"]["choices_detail"] if item["current"]]
        self.assertEqual(current_choices, ["codex-alt"])
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
            settings = json.loads((Path(tmp) / ".cc-switch" / "settings.json").read_text(encoding="utf-8"))

        self.assertEqual(switched["provider"], "alt")
        self.assertTrue(switched["changed"])
        self.assertTrue(switched["verified"])
        self.assertEqual(switched["method"], "ccswitch_settings_and_db_state")
        self.assertFalse(switched["secret_access"])
        self.assertIn("does not read", switched["warning"])
        self.assertEqual(summary["providers"]["codex"]["current"], "alt")
        self.assertEqual(summary["providers"]["codex"]["current_id"], "codex-alt")
        self.assertEqual(summary["providers"]["codex"]["current_source"], "settings")
        self.assertEqual(settings["currentProviderCodex"], "codex-alt")
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

    def test_reads_codex_runtime_for_local_gateway_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / ".cc-switch"
            db_dir.mkdir()
            db_path = db_dir / "cc-switch.db"
            settings_config = {
                "auth": {"OPENAI_API_KEY": "sk-secret"},
                "config": (
                    'model_provider = "dmxapi"\n'
                    'model = "gpt-5.4"\n'
                    "\n"
                    "[model_providers.dmxapi]\n"
                    'base_url = "https://www.dmxapi.com/v1"\n'
                    'wire_api = "responses"\n'
                    "requires_openai_auth = true\n"
                ),
            }
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, settings_config TEXT, is_current BOOLEAN DEFAULT 0, PRIMARY KEY(id, app_type))"
            )
            conn.execute("INSERT INTO providers VALUES (?, ?, ?, ?, ?)", ("codex-dmx", "codex", "DMXAPI", json.dumps(settings_config), 1))
            conn.commit()
            conn.close()
            (db_dir / "settings.json").write_text(json.dumps({"currentProviderCodex": "codex-dmx"}), encoding="utf-8")

            runtime = read_codex_provider_runtime(home=tmp)

        self.assertEqual(runtime["provider"], "DMXAPI")
        self.assertEqual(runtime["base_url"], "https://www.dmxapi.com/v1")
        self.assertEqual(runtime["api_key"], "sk-secret")
        self.assertEqual(runtime["model"], "gpt-5.4")
        self.assertTrue(runtime["secret_access"])
        self.assertEqual(runtime["secret_scope"], "local_gateway_only")

    def test_sqlite_truthy_handles_text_boolean_values(self):
        self.assertTrue(sqlite_truthy("1"))
        self.assertTrue(sqlite_truthy("true"))
        self.assertTrue(sqlite_truthy(1))
        self.assertFalse(sqlite_truthy("0"))
        self.assertFalse(sqlite_truthy("false"))
        self.assertFalse(sqlite_truthy(0))


if __name__ == "__main__":
    unittest.main()
