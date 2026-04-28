import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def test_scoped_probe_does_not_resolve_other_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alerts_state.json"
            alpha_alert = {"server": "alpha", "type": "offline", "severity": "error", "message": "ssh failed"}
            beta_alert = {"server": "beta", "type": "disk_warning", "severity": "warning", "message": "Disk / is 93% used."}
            apply_alert_state([alpha_alert, beta_alert], path=path)

            apply_alert_state([], path=path, scoped_servers={"alpha"})

            records = {record["server"]: record for record in all_alert_records(path=path)}
            self.assertEqual(records["alpha"]["status"], "resolved")
            self.assertEqual(records["beta"]["status"], "active")

    def test_parallel_alert_writes_do_not_share_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alerts_state.json"
            alerts = [
                [{"server": "alpha", "type": "offline", "severity": "error", "message": "ssh failed"}],
                [{"server": "beta", "type": "disk_warning", "severity": "warning", "message": "Disk / is 93% used."}],
            ]
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda item: apply_alert_state(item, path=path), alerts))

            records = all_alert_records(path=path)
            self.assertTrue(records)


if __name__ == "__main__":
    unittest.main()
