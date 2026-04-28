import os
import tempfile
import unittest
from pathlib import Path

from labgpu.remote.dashboard import (
    filter_available_gpu_items,
    filter_gpu_items,
    format_memory,
    gpu_recommendation,
    process_state_label,
    render_alerts_page,
    render_available_gpus,
    render_gpus_page,
    render_index,
    render_me_page,
    render_servers_page,
    render_settings_page,
    server_health,
)
from labgpu.remote.state import annotate_server, build_overview


def sample_data(shared_account: bool = False):
    server = annotate_server(
        {
            "alias": "alpha_liu",
            "hostname": "192.168.1.17",
            "remote_hostname": "a100",
            "online": True,
            "mode": "agentless",
            "tags": ["A100", "training"],
            "shared_account": shared_account,
            "allow_stop_own_process": True,
            "load_avg": {"1m": 8.9},
            "cpu_cores": 128,
            "memory": {"mem": {"used_percent": 7}, "swap": {"used_percent": 21}},
            "disks": [{"mount": "/", "use_percent": "93%"}],
            "gpus": [
                {
                    "index": 0,
                    "uuid": "GPU-free",
                    "name": "NVIDIA A100-SXM4-80GB",
                    "memory_total_mb": 81920,
                    "memory_used_mb": 0,
                    "memory_free_mb": 81920,
                    "utilization_gpu": 0,
                    "temperature": 31,
                    "processes": [],
                },
                {
                    "index": 1,
                    "uuid": "GPU-busy",
                    "name": "NVIDIA A100-SXM4-80GB",
                    "memory_total_mb": 81920,
                    "memory_used_mb": 60000,
                    "memory_free_mb": 21920,
                    "utilization_gpu": 0,
                    "temperature": 45,
                    "processes": [
                        {
                            "pid": 123,
                            "gpu_uuid": "GPU-busy",
                            "gpu_index": 1,
                            "user": "lsg",
                            "is_current_user": True,
                            "runtime_seconds": 3600,
                            "used_memory_mb": 60000,
                            "state": "S",
                            "cpu_percent": 0.1,
                            "command": "python train.py",
                            "start_time": "Tue Apr 28 01:00:00 2026",
                            "command_hash": "abc123",
                        }
                    ],
                },
            ],
            "processes": [
                {
                    "pid": 123,
                    "gpu_uuid": "GPU-busy",
                    "gpu_index": 1,
                    "user": "lsg",
                    "is_current_user": True,
                    "runtime_seconds": 3600,
                    "used_memory_mb": 60000,
                    "state": "S",
                    "cpu_percent": 0.1,
                    "command": "python train.py",
                    "start_time": "Tue Apr 28 01:00:00 2026",
                    "command_hash": "abc123",
                }
            ],
        }
    )
    overview = build_overview([server])
    for alert in overview["alert_items"]:
        alert.setdefault("status", "active")
        alert.setdefault("key", f"{alert['server']}-{alert['type']}")
    overview["all_alert_items"] = list(overview["alert_items"])
    return {"hosts": [server], "count": 1, "overview": overview, "ui": {}}


class DashboardPagesTest(unittest.TestCase):
    def test_gpus_page_and_filter(self):
        data = sample_data()
        html = render_gpus_page(data)
        self.assertIn("Find GPUs", html)
        self.assertIn("theme-toggle", html)
        self.assertIn("Notify me when GPU is free", html)
        self.assertIn("CUDA_VISIBLE_DEVICES=0", html)
        filtered = filter_available_gpu_items(data["overview"]["available_gpu_items"], {"min_mem_gb": "80", "model": "A100"})
        self.assertEqual(filtered[0]["server"], "alpha_liu")
        choices = filter_gpu_items(data["overview"]["gpu_items"], {"availability": "all"})
        self.assertEqual(len(choices), 2)
        self.assertEqual(gpu_recommendation(choices[0])["label"], "OK")

    def test_my_processes_page_shows_health_and_action_guard(self):
        html = render_me_page(sample_data(shared_account=True))
        self.assertIn("My GPU Processes", html)
        self.assertIn("suspected_idle", html)
        self.assertIn("shared account", html)
        self.assertIn("Expand", html)
        self.assertIn("58.6 GB", html)

    def test_servers_and_alerts_pages(self):
        server_html = render_servers_page(sample_data())
        alerts_html = render_alerts_page(sample_data())
        self.assertIn("Servers", server_html)
        self.assertIn("alpha_liu", server_html)
        self.assertIn("online · warning", server_html)
        self.assertIn("All Alerts", alerts_html)
        self.assertIn("disk_warning", alerts_html)
        self.assertIn("Dismiss", alerts_html)

    def test_settings_page_lists_ssh_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("LABGPU_HOME")
            os.environ["LABGPU_HOME"] = tmp
            ssh_config = Path(tmp) / "ssh_config"
            ssh_config.write_text("Host alpha_liu\n  HostName 192.168.1.17\n  User lsg\n", encoding="utf-8")
            try:
                html = render_settings_page(ssh_config=ssh_config)
            finally:
                if old_home is None:
                    os.environ.pop("LABGPU_HOME", None)
                else:
                    os.environ["LABGPU_HOME"] = old_home
            self.assertIn("Import From SSH Config", html)
            self.assertIn("alpha_liu", html)
            self.assertIn("Save selected hosts", html)

    def test_summary_cards_and_empty_gpu_state(self):
        data = sample_data()
        data["overview"]["available_gpu_items"] = []
        data["overview"]["available_gpus"] = 0
        html = render_index(data)
        self.assertIn("summary-card warning", html)
        empty = render_available_gpus([], {}, counts=data["overview"])
        self.assertIn("No clearly free GPU found.", empty)
        self.assertIn("View busy GPUs", empty)

    def test_formatters_and_server_health(self):
        self.assertEqual(format_memory(60000), "58.6 GB")
        self.assertEqual(process_state_label("R+"), "running")
        self.assertEqual(process_state_label("D"), "io wait")
        self.assertEqual(server_health(sample_data()["hosts"][0]), "warning")


if __name__ == "__main__":
    unittest.main()
