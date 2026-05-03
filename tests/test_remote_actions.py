import unittest
from pathlib import Path
from unittest.mock import patch

from labgpu.remote.actions import build_ssh_terminal_argv, is_safe_ssh_alias, open_ssh_terminal, stop_process
from labgpu.remote.ssh_config import SSHHost


SESSION_TOKEN = "labgpu-session-abcdefghijklmnopqrstuvwxyz012345"


class FakeGateway:
    token = SESSION_TOKEN
    listen_port = 49231
    token_fingerprint = "012345"

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


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
        self.assertEqual(argv[:6], ["ssh", "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:7890:127.0.0.1:7890", "-t"])
        self.assertEqual(argv[6], "alpha_liu")
        self.assertIn("HTTP_PROXY=http://127.0.0.1:7890", argv[7])
        self.assertIn("codex", argv[7])
        self.assertIn("-ic", argv[7])
        self.assertNotIn("ALL_PROXY", argv[7])

    def test_build_ssh_terminal_command_splits_local_and_remote_proxy_ports(self):
        argv = build_ssh_terminal_argv("alpha_liu", local_proxy_port="33210", remote_proxy_port="43310", agent="codex")
        self.assertEqual(argv[:6], ["ssh", "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:43310:127.0.0.1:33210", "-t"])
        self.assertEqual(argv[6], "alpha_liu")
        self.assertIn("HTTP_PROXY=http://127.0.0.1:43310", argv[7])
        self.assertIn("local 127.0.0.1:33210", argv[7])

    def test_build_ssh_terminal_command_auto_selects_remote_proxy_port(self):
        with patch("labgpu.remote.actions.random.randint", return_value=51234):
            argv = build_ssh_terminal_argv("alpha_liu", local_proxy_port="15721", agent="codex")
        self.assertEqual(argv[:6], ["ssh", "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:51234:127.0.0.1:15721", "-t"])
        self.assertIn("HTTP_PROXY=http://127.0.0.1:51234", argv[7])

    def test_build_ssh_terminal_command_adds_network_proxy_tunnel(self):
        argv = build_ssh_terminal_argv(
            "alpha_liu",
            network_local_proxy_port="7890",
            network_remote_proxy_port="45678",
            network_proxy_scheme="socks5",
        )
        self.assertEqual(argv[:6], ["ssh", "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:45678:127.0.0.1:7890", "-t"])
        self.assertIn("HTTP_PROXY=socks5://127.0.0.1:45678", argv[7])
        self.assertIn("ALL_PROXY=socks5://127.0.0.1:45678", argv[7])
        self.assertIn("LabGPU network tunnel", argv[7])

    def test_build_ssh_terminal_command_combines_ai_and_network_tunnels(self):
        argv = build_ssh_terminal_argv(
            "alpha_liu",
            local_proxy_port="15721",
            remote_proxy_port="27183",
            agent="claude",
            ai_mode="proxy_tunnel",
            provider_name="PackyCode",
            local_gateway_port="49231",
            session_token=SESSION_TOKEN,
            network_local_proxy_port="7890",
            network_remote_proxy_port="45678",
        )
        self.assertIn("127.0.0.1:27183:127.0.0.1:49231", argv)
        self.assertIn("127.0.0.1:45678:127.0.0.1:7890", argv)
        self.assertIn("HTTP_PROXY=http://127.0.0.1:45678", argv[-1])

    def test_build_ssh_terminal_command_for_claude_ai_proxy_tunnel(self):
        argv = build_ssh_terminal_argv(
            "alpha_liu",
            local_proxy_port="15721",
            remote_proxy_port="15721",
            agent="claude",
            ai_mode="proxy_tunnel",
            provider_name="PackyCode",
            gpu_index="0",
            remote_cwd="/data/lsg/work/OPSD",
            local_gateway_port="49231",
            session_token=SESSION_TOKEN,
        )
        self.assertEqual(argv[:6], ["ssh", "-tt", "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:15721:127.0.0.1:49231"])
        self.assertEqual(argv[6], "alpha_liu")
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:15721", argv[7])
        self.assertIn(f"ANTHROPIC_API_KEY={SESSION_TOKEN}", argv[7])
        self.assertIn("CUDA_VISIBLE_DEVICES=0", argv[7])
        self.assertIn("LABGPU_REMOTE_CWD=/data/lsg/work/OPSD", argv[7])
        self.assertIn("cd /data/lsg/work/OPSD || exit 1", argv[7])
        self.assertNotIn("SECRET", " ".join(argv))

    def test_build_ssh_terminal_command_for_codex_ai_proxy_tunnel(self):
        argv = build_ssh_terminal_argv(
            "alpha_liu",
            local_proxy_port="15721",
            remote_proxy_port="27183",
            agent="codex",
            ai_mode="proxy_tunnel",
            provider_name="OpenAI",
            remote_cwd="/data/lsg/work/OPSD",
            local_gateway_port="49231",
            session_token=SESSION_TOKEN,
        )
        self.assertEqual(argv[:6], ["ssh", "-tt", "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:27183:127.0.0.1:49231"])
        self.assertEqual(argv[6], "alpha_liu")
        self.assertIn("LABGPU_AI_APP=codex", argv[7])
        self.assertIn("OPENAI_BASE_URL=http://127.0.0.1:27183", argv[7])
        self.assertIn(f"OPENAI_API_KEY={SESSION_TOKEN}", argv[7])
        self.assertIn("CODEX_HOME", argv[7])
        self.assertIn("auth.json", argv[7])
        self.assertNotIn("~/.codex", argv[7])
        self.assertNotIn("SECRET", " ".join(argv))

    def test_build_ssh_terminal_command_uses_codex_command_override(self):
        host = SSHHost(alias="alpha_liu", codex_command="~/.local/bin/codex")
        argv = build_ssh_terminal_argv(
            "alpha_liu",
            host=host,
            local_proxy_port="15721",
            remote_proxy_port="27183",
            agent="codex",
            ai_mode="proxy_tunnel",
            provider_name="OpenAI",
            local_gateway_port="49231",
            session_token=SESSION_TOKEN,
        )
        self.assertIn("LABGPU_AI_CODEX_COMMAND='~/.local/bin/codex'", argv[-1])
        self.assertIn('LABGPU_REAL_CODEX="${LABGPU_AI_CODEX_COMMAND:-}"', argv[-1])

    def test_build_ssh_terminal_command_for_remote_config_override(self):
        argv = build_ssh_terminal_argv(
            "alpha_liu",
            local_proxy_port="15721",
            remote_proxy_port="27183",
            agent="claude",
            ai_mode="remote_write",
            provider_name="DMXAPI",
            local_gateway_port="49231",
            session_token=SESSION_TOKEN,
        )
        self.assertEqual(argv[:6], ["ssh", "-tt", "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:27183:127.0.0.1:49231"])
        self.assertIn("LABGPU_AI_MODE=remote_write", argv[7])
        self.assertIn("$HOME/.claude/settings.json", argv[7])
        self.assertIn("LABGPU_REMOTE_WRITE_BACKUP", argv[7])
        self.assertIn("labgpu-session-", argv[7])

    def test_build_ssh_terminal_command_can_isolate_config_forwardings(self):
        host = SSHHost(alias="alpha_liu", hostname="210.45.70.34", user="lsg", port="22", options={"remoteforward": "127.0.0.1:29890 127.0.0.1:33210"})
        argv = build_ssh_terminal_argv(
            "alpha_liu",
            host=host,
            local_proxy_port="15721",
            remote_proxy_port="15721",
            agent="claude",
            ai_mode="proxy_tunnel",
            provider_name="PackyCode",
            local_gateway_port="49231",
            session_token=SESSION_TOKEN,
        )
        command = " ".join(argv)
        self.assertIn("-F /dev/null", command)
        self.assertIn("HostName=210.45.70.34", command)
        self.assertIn("User=lsg", command)
        self.assertIn("Port=22", command)
        self.assertNotIn("29890", command)
        self.assertIn("127.0.0.1:15721:127.0.0.1:49231", command)

    def test_open_ssh_terminal_reports_claude_proxy_not_listening(self):
        host = SSHHost(alias="alpha_liu")
        with (
            patch("labgpu.remote.actions.is_local_tcp_port_open", return_value=False),
            patch("labgpu.remote.actions.subprocess.run") as run,
            patch("labgpu.remote.actions.append_audit"),
        ):
            result = open_ssh_terminal(
                host,
                local_proxy_port="15721",
                remote_proxy_port="15721",
                agent="claude",
                ai_mode="proxy_tunnel",
                provider_name="PackyCode",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "local_proxy_not_listening")
        self.assertIn("CC Switch Claude Code proxy is configured but not listening on 127.0.0.1:15721", result["message"])
        run.assert_not_called()

    def test_open_ssh_terminal_starts_gateway_for_codex_proxy_tunnel(self):
        host = SSHHost(alias="alpha_liu")
        gateway = FakeGateway()

        class Result:
            returncode = 0
            stderr = ""

        with (
            patch("labgpu.remote.actions.sys.platform", "darwin"),
            patch("labgpu.remote.actions.is_local_tcp_port_open", return_value=True),
            patch("labgpu.remote.actions.start_ai_gateway", return_value=gateway) as start_gateway,
            patch("labgpu.remote.actions.AI_GATEWAY_SESSIONS", []),
            patch("labgpu.remote.actions.write_terminal_launch_script", return_value=Path("/tmp/labgpu-open.sh")),
            patch("labgpu.remote.actions.subprocess.run", return_value=Result()),
            patch("labgpu.remote.actions.append_audit"),
        ):
            result = open_ssh_terminal(
                host,
                local_proxy_port="15721",
                remote_proxy_port="27183",
                agent="codex",
                ai_mode="proxy_tunnel",
                provider_name="OpenAI",
            )
        self.assertTrue(result["ok"])
        metadata = start_gateway.call_args.kwargs["metadata"]
        self.assertEqual(metadata["app"], "codex")
        self.assertEqual(metadata["provider"], "OpenAI")
        self.assertEqual(result["ai_gateway"]["ccswitch_proxy_port"], 15721)
        self.assertNotIn(SESSION_TOKEN, result["command"])

    def test_open_ssh_terminal_reports_network_proxy_not_listening(self):
        host = SSHHost(alias="alpha_liu")
        with (
            patch("labgpu.remote.actions.is_local_tcp_port_open", return_value=False),
            patch("labgpu.remote.actions.subprocess.run") as run,
            patch("labgpu.remote.actions.append_audit"),
        ):
            result = open_ssh_terminal(
                host,
                agent="claude",
                ai_mode="proxy_tunnel",
                provider_name="PackyCode",
                local_proxy_port="15721",
                remote_proxy_port="27183",
                network_proxy_enabled=True,
                network_local_proxy_port="7890",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "local_proxy_not_listening")
        run.assert_not_called()

        def port_open(port):
            return int(port) == 15721

        with (
            patch("labgpu.remote.actions.is_local_tcp_port_open", side_effect=port_open),
            patch("labgpu.remote.actions.subprocess.run") as run,
            patch("labgpu.remote.actions.append_audit"),
        ):
            result = open_ssh_terminal(
                host,
                agent="claude",
                ai_mode="proxy_tunnel",
                provider_name="PackyCode",
                local_proxy_port="15721",
                remote_proxy_port="27183",
                network_proxy_enabled=True,
                network_local_proxy_port="7890",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "network_proxy_not_listening")
        self.assertIn("127.0.0.1:7890", result["message"])
        run.assert_not_called()

    def test_open_ssh_terminal_returns_network_tunnel_metadata(self):
        host = SSHHost(alias="alpha_liu")
        gateway = FakeGateway()

        class Result:
            returncode = 0
            stderr = ""

        with (
            patch("labgpu.remote.actions.sys.platform", "darwin"),
            patch("labgpu.remote.actions.is_local_tcp_port_open", return_value=True),
            patch("labgpu.remote.actions.start_ai_gateway", return_value=gateway),
            patch("labgpu.remote.actions.AI_GATEWAY_SESSIONS", []),
            patch("labgpu.remote.actions.write_terminal_launch_script", return_value=Path("/tmp/labgpu-open.sh")),
            patch("labgpu.remote.actions.subprocess.run", return_value=Result()),
            patch("labgpu.remote.actions.append_audit"),
        ):
            result = open_ssh_terminal(
                host,
                local_proxy_port="15721",
                remote_proxy_port="27183",
                agent="codex",
                ai_mode="proxy_tunnel",
                provider_name="OpenAI",
                network_proxy_enabled=True,
                network_local_proxy_port="7890",
                network_remote_proxy_port="45678",
                network_proxy_scheme="socks5",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["network_tunnel"]["local_proxy_port"], 7890)
        self.assertEqual(result["network_tunnel"]["remote_proxy_port"], 45678)
        self.assertEqual(result["network_tunnel"]["proxy_url"], "socks5://127.0.0.1:45678")

    def test_open_ssh_terminal_starts_gateway_for_remote_config_override(self):
        host = SSHHost(alias="alpha_liu")
        gateway = FakeGateway()

        class Result:
            returncode = 0
            stderr = ""

        with (
            patch("labgpu.remote.actions.sys.platform", "darwin"),
            patch("labgpu.remote.actions.is_local_tcp_port_open", return_value=True),
            patch("labgpu.remote.actions.start_ai_gateway", return_value=gateway) as start_gateway,
            patch("labgpu.remote.actions.AI_GATEWAY_SESSIONS", []),
            patch("labgpu.remote.actions.write_terminal_launch_script", return_value=Path("/tmp/labgpu-open.sh")),
            patch("labgpu.remote.actions.subprocess.run", return_value=Result()),
            patch("labgpu.remote.actions.append_audit"),
        ):
            result = open_ssh_terminal(
                host,
                local_proxy_port="15721",
                remote_proxy_port="27183",
                agent="claude",
                ai_mode="remote_write",
                provider_name="DMXAPI",
            )
        self.assertTrue(result["ok"])
        metadata = start_gateway.call_args.kwargs["metadata"]
        self.assertEqual(metadata["mode"], "remote_write")
        self.assertEqual(metadata["app"], "claude")
        self.assertEqual(result["ai_gateway"]["ccswitch_proxy_port"], 15721)
        self.assertNotIn(SESSION_TOKEN, result["command"])

    def test_open_ssh_terminal_mentions_remote_proxy_port_conflict(self):
        host = SSHHost(alias="alpha_liu")
        gateway = FakeGateway()

        class Result:
            returncode = 0
            stderr = ""

        with (
            patch("labgpu.remote.actions.sys.platform", "darwin"),
            patch("labgpu.remote.actions.is_local_tcp_port_open", return_value=True),
            patch("labgpu.remote.actions.start_ai_gateway", return_value=gateway) as start_gateway,
            patch("labgpu.remote.actions.AI_GATEWAY_SESSIONS", []),
            patch("labgpu.remote.actions.write_terminal_launch_script", return_value=Path("/tmp/labgpu-open.sh")) as write_script,
            patch("labgpu.remote.actions.subprocess.run", return_value=Result()) as run,
            patch("labgpu.remote.actions.append_audit"),
        ):
            result = open_ssh_terminal(
                host,
                local_proxy_port="15721",
                remote_proxy_port="27183",
                agent="claude",
                ai_mode="proxy_tunnel",
                provider_name="PackyCode",
            )
        self.assertTrue(result["ok"])
        self.assertIn("remote gateway port 27183 may already be in use", result["message"])
        self.assertEqual(result["ai_gateway"]["local_gateway_port"], 49231)
        self.assertEqual(result["ai_gateway"]["remote_gateway_port"], 27183)
        self.assertEqual(result["ai_gateway"]["ccswitch_proxy_port"], 15721)
        self.assertNotIn(SESSION_TOKEN, result["command"])
        self.assertIn("labgpu-session-<redacted>", result["command"])
        metadata = start_gateway.call_args.kwargs["metadata"]
        self.assertEqual(metadata["mode"], "proxy_tunnel")
        self.assertEqual(metadata["app"], "claude")
        self.assertEqual(metadata["provider"], "PackyCode")
        self.assertEqual(metadata["server"], "alpha_liu")
        self.assertEqual(metadata["ccswitch_proxy_port"], 15721)
        write_script.assert_called_once()
        osascript_command = " ".join(run.call_args.args[0])
        self.assertIn("/tmp/labgpu-open.sh", osascript_command)
        self.assertNotIn(SESSION_TOKEN, osascript_command)
        self.assertFalse(gateway.closed)

    def test_open_ssh_terminal_redacts_token_and_closes_gateway_on_terminal_failure(self):
        host = SSHHost(alias="alpha_liu")
        gateway = FakeGateway()

        class Result:
            returncode = 1
            stderr = "terminal failed"

        with (
            patch("labgpu.remote.actions.sys.platform", "darwin"),
            patch("labgpu.remote.actions.is_local_tcp_port_open", return_value=True),
            patch("labgpu.remote.actions.start_ai_gateway", return_value=gateway),
            patch("labgpu.remote.actions.AI_GATEWAY_SESSIONS", []),
            patch("labgpu.remote.actions.write_terminal_launch_script", return_value=Path("/tmp/labgpu-open.sh")),
            patch("labgpu.remote.actions.subprocess.run", return_value=Result()) as run,
            patch("labgpu.remote.actions.append_audit") as audit,
        ):
            result = open_ssh_terminal(
                host,
                local_proxy_port="15721",
                remote_proxy_port="27183",
                agent="claude",
                ai_mode="proxy_tunnel",
                provider_name="PackyCode",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "open_failed")
        self.assertNotIn(SESSION_TOKEN, " ".join(run.call_args.args[0]))
        self.assertNotIn(SESSION_TOKEN, result["command"])
        self.assertIn("labgpu-session-<redacted>", result["command"])
        self.assertTrue(gateway.closed)
        self.assertNotIn(SESSION_TOKEN, str(audit.call_args_list))

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

        codex = build_ssh_terminal_argv("alpha_liu", host=SSHHost(alias="alpha_liu", codex_command="~/.local/bin/codex"), agent="codex")
        self.assertIn("~/.local/bin/codex", codex[3])

    def test_build_ssh_terminal_rejects_bad_options(self):
        with self.assertRaises(ValueError):
            build_ssh_terminal_argv("alpha_liu", proxy_port="99999")
        with self.assertRaises(ValueError):
            build_ssh_terminal_argv("alpha_liu", agent="shell")
        with self.assertRaisesRegex(ValueError, "Only Claude Code and Codex CLI"):
            build_ssh_terminal_argv(
                "alpha_liu",
                local_proxy_port="15721",
                remote_proxy_port="27183",
                agent="gemini",
                ai_mode="remote_write",
                provider_name="Gemini",
                local_gateway_port="49231",
                session_token=SESSION_TOKEN,
            )


if __name__ == "__main__":
    unittest.main()
