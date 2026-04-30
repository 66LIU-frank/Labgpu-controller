import unittest
from unittest.mock import patch

from labgpu.remote.actions import build_ssh_terminal_argv, is_safe_ssh_alias, open_ssh_terminal, stop_process
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

    def test_shared_account_disables_agentless_stop(self):
        host = SSHHost(alias="alpha", shared_account=True)
        with patch("labgpu.remote.actions.probe_host") as probe, patch("labgpu.remote.actions.subprocess.run") as run, patch("labgpu.remote.actions.append_audit"):
            result = stop_process(
                host,
                pid=123,
                expected_user="alice",
                expected_start_time=None,
                expected_command_hash=None,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "shared_account_disabled")
        probe.assert_not_called()
        run.assert_not_called()

    def test_open_ssh_terminal_uses_macos_terminal_without_shell(self):
        host = SSHHost(alias="alpha_liu")

        class Result:
            returncode = 0
            stderr = ""

        with (
            patch("labgpu.remote.actions.sys.platform", "darwin"),
            patch("labgpu.remote.actions.subprocess.run", return_value=Result()) as run,
            patch("labgpu.remote.actions.append_audit"),
        ):
            result = open_ssh_terminal(host)

        self.assertTrue(result["ok"])
        self.assertEqual(result["command"], "ssh alpha_liu")
        args = run.call_args.args[0]
        self.assertEqual(args[0], "osascript")
        self.assertIn("ssh alpha_liu", " ".join(args))

    def test_open_ssh_terminal_rejects_unsafe_alias(self):
        self.assertFalse(is_safe_ssh_alias("-oProxyCommand=bad"))
        self.assertFalse(is_safe_ssh_alias("alpha;rm"))
        with patch("labgpu.remote.actions.subprocess.run") as run, patch("labgpu.remote.actions.subprocess.Popen") as popen:
            result = open_ssh_terminal(SSHHost(alias="-oProxyCommand=bad"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "invalid_alias")
        run.assert_not_called()
        popen.assert_not_called()

    def test_build_ssh_terminal_command_with_proxy_and_agent(self):
        argv = build_ssh_terminal_argv("alpha_liu", proxy_port="7890", agent="codex")
        self.assertEqual(argv[:4], ["ssh", "-R", "127.0.0.1:7890:127.0.0.1:7890", "-t"])
        self.assertEqual(argv[4], "alpha_liu")
        self.assertIn("HTTP_PROXY=http://127.0.0.1:7890", argv[5])
        self.assertIn("codex", argv[5])
        self.assertIn("-lc", argv[5])
        self.assertNotIn("ALL_PROXY", argv[5])

    def test_build_ssh_terminal_command_supports_agent_launchers(self):
        gemini = build_ssh_terminal_argv("alpha_liu", agent="gemini")
        self.assertEqual(gemini[:3], ["ssh", "-t", "alpha_liu"])
        self.assertIn("gemini", gemini[3])
        self.assertIn("Gemini CLI was not found.", gemini[3])

        openclaw = build_ssh_terminal_argv("alpha_liu", agent="openclaw")
        self.assertIn("openclaw agent", openclaw[3])
        self.assertIn("OpenClaw CLI was not found.", openclaw[3])

        claude = build_ssh_terminal_argv("alpha_liu", agent="claude-code")
        self.assertIn("claude-code", claude[3])

    def test_build_ssh_terminal_rejects_bad_options(self):
        with self.assertRaises(ValueError):
            build_ssh_terminal_argv("alpha_liu", proxy_port="99999")
        with self.assertRaises(ValueError):
            build_ssh_terminal_argv("alpha_liu", agent="shell")


if __name__ == "__main__":
    unittest.main()
