import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from labgpu.core.config import LabGPUConfig, ServerEntry, write_config
from labgpu.remote.dashboard import (
    ServerHandler,
    collect_servers,
    filter_available_gpu_items,
    filter_gpu_items,
    format_memory,
    gpu_recommendation,
    process_state_label,
    render_alerts_page,
    render_assistant_page,
    render_available_gpus,
    render_gpus_page,
    render_groups_page,
    render_host_card,
    render_index,
    render_me_page,
    render_servers_page,
    render_settings_page,
    server_health,
)
from labgpu.remote.cache import write_server_cache
from labgpu.remote.state import annotate_server, build_overview


def sample_data(shared_account: bool = False):
    server = annotate_server(
        {
            "alias": "alpha_liu",
            "hostname": "192.168.1.17",
            "remote_hostname": "a100",
            "online": True,
            "mode": "agentless",
            "group": "AlphaLab",
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
        old_allowed, old_fake = ServerHandler.action_allowed, ServerHandler.fake_lab
        ServerHandler.action_allowed = True
        ServerHandler.fake_lab = False
        try:
            html = render_gpus_page(data)
        finally:
            ServerHandler.action_allowed = old_allowed
            ServerHandler.fake_lab = old_fake
        self.assertIn("Train Now", html)
        self.assertIn("language-toggle", html)
        self.assertIn("theme-toggle", html)
        self.assertIn("Notify me when GPU is free", html)
        self.assertIn("CUDA_VISIBLE_DEVICES=0", html)
        self.assertIn("Open SSH terminal", html)
        self.assertIn('data-open-ssh="alpha_liu"', html)
        filtered = filter_available_gpu_items(data["overview"]["available_gpu_items"], {"min_mem_gb": "80", "model": "A100"})
        self.assertEqual(filtered[0]["server"], "alpha_liu")
        self.assertEqual(filtered[0]["server_group"], "AlphaLab")
        choices = filter_gpu_items(data["overview"]["gpu_items"], {"availability": "all"})
        self.assertEqual(len(choices), 2)
        self.assertEqual(gpu_recommendation(choices[0])["label"], "Recommended")

    def test_my_processes_page_shows_health_and_action_guard(self):
        html = render_me_page(sample_data(shared_account=True))
        self.assertIn("My Training", html)
        self.assertIn("Agentless Own GPU Processes", html)
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

    def test_assistant_page_has_read_only_chat(self):
        html = render_assistant_page(sample_data())
        self.assertIn("LabGPU Assistant", html)
        self.assertIn("/api/assistant/chat", html)
        self.assertIn("copyable plans", html)
        self.assertIn("data-assistant-example", html)
        self.assertIn("Assistant API", html)
        self.assertIn("assistant-use-api", html)
        self.assertIn("assistant-api-url", html)
        self.assertIn("assistant-model", html)

    def test_settings_page_lists_ssh_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("LABGPU_HOME")
            os.environ["LABGPU_HOME"] = tmp
            ssh_config = Path(tmp) / "ssh_config"
            ssh_config.write_text("Host alpha_liu\n  HostName 192.168.1.17\n  User lsg\n", encoding="utf-8")
            config = LabGPUConfig()
            config.servers["alpha_liu"] = ServerEntry(name="alpha_liu", alias="alpha_liu", tags=["A100"])
            write_config(config)
            try:
                html = render_settings_page(ssh_config=ssh_config)
            finally:
                if old_home is None:
                    os.environ.pop("LABGPU_HOME", None)
                else:
                    os.environ["LABGPU_HOME"] = old_home
            self.assertIn("Import From SSH Config", html)
            self.assertIn("Add Server", html)
            self.assertIn("settings-add-server", html)
            self.assertIn("Manage groups", html)
            self.assertIn("Server Groups", html)
            self.assertIn("Group", html)
            self.assertIn("Write to SSH config", html)
            self.assertIn("alpha_liu", html)
            self.assertIn("value='alpha_liu' checked", html)
            self.assertIn("Save selected hosts", html)

    def test_groups_page_assigns_saved_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("LABGPU_HOME")
            os.environ["LABGPU_HOME"] = tmp
            config = LabGPUConfig()
            config.servers["alpha_liu"] = ServerEntry(name="alpha_liu", alias="alpha_liu", group="AlphaLab", tags=["A100"])
            config.servers["Song-1"] = ServerEntry(name="Song-1", alias="Song-1", tags=["4090"])
            write_config(config)
            try:
                html = render_groups_page()
            finally:
                if old_home is None:
                    os.environ.pop("LABGPU_HOME", None)
                else:
                    os.environ["LABGPU_HOME"] = old_home
            self.assertIn("Existing Groups", html)
            self.assertIn("AlphaLab", html)
            self.assertIn("settings-groups", html)
            self.assertIn("Group name", html)
            self.assertIn("Song-1", html)

    def test_cached_ui_collection_does_not_probe_before_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("LABGPU_HOME")
            os.environ["LABGPU_HOME"] = tmp
            config = LabGPUConfig()
            config.servers["alpha_liu"] = ServerEntry(name="alpha_liu", alias="alpha_liu", enabled=True, tags=["A100"])
            write_config(config)
            cached = sample_data()["hosts"][0]
            cached["probed_at"] = "2026-04-28T12:00:00+00:00"
            write_server_cache(cached)
            try:
                with patch("labgpu.remote.dashboard.probe_host", side_effect=AssertionError("should not probe")):
                    data = collect_servers(use_cache=True, background_refresh=False)
            finally:
                if old_home is None:
                    os.environ.pop("LABGPU_HOME", None)
                else:
                    os.environ["LABGPU_HOME"] = old_home
        self.assertEqual(data["hosts"][0]["alias"], "alpha_liu")
        self.assertTrue(data["hosts"][0]["from_cache"])
        self.assertEqual(data["cache_mode"], "snapshot")
        html = render_index(data)
        self.assertIn("Cached page", html)
        self.assertIn("Cached data age", html)
        self.assertNotIn("Opening from local cache", html)
        self.assertIn("Refresh now", html)

    def test_summary_cards_and_empty_gpu_state(self):
        data = sample_data()
        data["overview"]["available_gpu_items"] = []
        data["overview"]["available_gpus"] = 0
        html = render_index(data)
        self.assertIn("summary-card warning", html)
        empty = render_available_gpus([], {}, counts=data["overview"])
        self.assertIn("No clearly free GPU found.", empty)
        self.assertIn("View busy GPUs", empty)

    def test_home_server_list_is_not_capped_at_six(self):
        data = sample_data()
        hosts = []
        for index in range(7):
            host = dict(data["hosts"][0])
            host["alias"] = f"host_{index}"
            hosts.append(host)
        data["hosts"] = hosts
        data["overview"] = build_overview(hosts)
        html = render_index(data)
        self.assertIn("host_0", html)
        self.assertIn("host_6", html)
        self.assertIn("Choose home servers", html)

    def test_group_bar_filters_home_view(self):
        data = sample_data()
        data["server_groups"] = [{"value": "AlphaLab", "label": "AlphaLab"}]
        data["scope_group"] = "AlphaLab"
        html = render_index(data)
        self.assertIn("Server group", html)
        self.assertIn("AlphaLab", html)
        self.assertIn("group AlphaLab", html)

    def test_offline_cached_server_is_labeled_as_cached(self):
        cached = sample_data()["hosts"][0]
        host = {
            "alias": "alpha_liu",
            "hostname": "10.0.0.1",
            "user": "lsg",
            "port": "22",
            "online": False,
            "mode": "offline",
            "error": "ssh probe timed out",
            "elapsed_ms": 12000,
            "probed_at": "2026-04-28T11:46:14+00:00",
            "last_seen": "2026-04-28T05:08:53+00:00",
            "cached": cached,
            "alerts": [{"severity": "error"}],
        }
        html = render_host_card(host)
        self.assertIn("offline · cached", html)
        self.assertIn("cached 2 GPUs", html)
        self.assertIn("Showing cached snapshot", html)
        self.assertIn("ssh probe timed out", html)

    def test_reachable_probe_timeout_uses_cached_label_without_offline(self):
        cached = sample_data()["hosts"][0]
        host = {
            "alias": "alpha_liu",
            "hostname": "10.0.0.1",
            "user": "lsg",
            "port": "22",
            "online": True,
            "mode": "stale",
            "probe_status": "probe_timeout",
            "probe_incomplete": True,
            "error": "GPU refresh timed out; SSH is reachable.",
            "elapsed_ms": 24000,
            "probed_at": "2026-04-28T11:46:14+00:00",
            "last_seen": "2026-04-28T05:08:53+00:00",
            "cached": cached,
            "alerts": [{"severity": "warning"}],
        }
        html = render_host_card(host)
        self.assertIn("online · cached", html)
        self.assertIn("cached 2 GPUs", html)
        self.assertIn("SSH is reachable", html)
        self.assertNotIn("offline · cached", html)

    def test_formatters_and_server_health(self):
        self.assertEqual(format_memory(60000), "58.6 GB")
        self.assertEqual(process_state_label("R+"), "running")
        self.assertEqual(process_state_label("D"), "io wait")
        self.assertEqual(server_health(sample_data()["hosts"][0]), "warning")


if __name__ == "__main__":
    unittest.main()
