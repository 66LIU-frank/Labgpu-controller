import unittest
from unittest.mock import patch

from labgpu.remote.actions import stop_process
from labgpu.remote.ssh_config import SSHHost


class RemoteActionsTest(unittest.TestCase):
    def test_refuses_other_users_process(self):
        host = SSHHost(alias="alpha")
        with (
            patch("labgpu.remote.actions.probe_host") as probe,
            patch("labgpu.remote.actions.subprocess.run") as run,
            patch("labgpu.remote.actions.append_audit"),
        ):
            probe.return_value = {
                "alias": "alpha",
                "online": True,
                "gpus": [],
                "processes": [{"pid": 123, "user": "bob", "is_current_user": False, "command_hash": "h"}],
            }
            result = stop_process(
                host,
                pid=123,
                expected_user="bob",
                expected_start_time=None,
                expected_command_hash="h",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "not_current_user")
        run.assert_not_called()

    def test_pid_reuse_guard(self):
        host = SSHHost(alias="alpha")
        with (
            patch("labgpu.remote.actions.probe_host") as probe,
            patch("labgpu.remote.actions.subprocess.run") as run,
            patch("labgpu.remote.actions.append_audit"),
        ):
            probe.return_value = {
                "alias": "alpha",
                "online": True,
                "gpus": [],
                "processes": [
                    {
                        "pid": 123,
                        "user": "alice",
                        "is_current_user": True,
                        "start_time": "new",
                        "command_hash": "newhash",
                    }
                ],
            }
            result = stop_process(
                host,
                pid=123,
                expected_user="alice",
                expected_start_time="old",
                expected_command_hash="oldhash",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "process_identity_changed")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
