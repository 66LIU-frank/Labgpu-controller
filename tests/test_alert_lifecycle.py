import tempfile
import unittest
from pathlib import Path

from labgpu.remote.alerts import all_alert_records, apply_alert_state, set_alert_status


class AlertLifecycleTest(unittest.TestCase):
    def test_alert_first_last_dismiss_and_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alerts_state.json"
            alerts = [{"server": "alpha", "type": "disk_warning", "severity": "warning", "message": "Disk / is 93% used."}]
            active = apply_alert_state(alerts, path=path)
            self.assertEqual(active[0]["status"], "active")
            self.assertIn("first_seen", active[0])
            key = active[0]["key"]

            dismissed = set_alert_status(key, "dismissed", path=path)
            self.assertEqual(dismissed["status"], "dismissed")

            resolved = apply_alert_state([], path=path)
            self.assertEqual(resolved, [])
            records = all_alert_records(path=path)
            self.assertEqual(records[0]["status"], "resolved")
            self.assertIn("resolved_at", records[0])


if __name__ == "__main__":
    unittest.main()
