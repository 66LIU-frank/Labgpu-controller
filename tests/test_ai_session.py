import unittest

from labgpu.remote.ai_session import EnterServerAIRequest, build_ai_ssh_command, build_claude_command_probe


SESSION_TOKEN = "labgpu-session-abcdefghijklmnopqrstuvwxyz012345"


class AISessionTest(unittest.TestCase):
    def test_build_ai_ssh_command_for_claude_proxy_tunnel(self):
        command = build_ai_ssh_command(
            EnterServerAIRequest(
                server_alias="alpha_liu",
                gpu_index="0",
                ai_app="claude",
                provider_name="PackyCode",
                ccswitch_proxy_port=15721,
                local_gateway_port=49231,
                remote_gateway_port=27183,
                session_token=SESSION_TOKEN,
            )
        )

        self.assertEqual(command.ssh_args[:6], ["ssh", "-tt", "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:27183:127.0.0.1:49231"])
        self.assertEqual(command.ssh_args[6], "alpha_liu")
        remote = command.ssh_args[7]
        self.assertIn("LABGPU_AI_MODE=proxy_tunnel", remote)
        self.assertIn("LABGPU_AI_APP=claude", remote)
        self.assertIn("LABGPU_AI_PROVIDER=PackyCode", remote)
        self.assertIn("PATH=${HOME}/miniconda3/bin:${HOME}/.local/bin:$PATH", remote)
        self.assertIn("LABGPU_CLAUDE_SETTINGS", remote)
        self.assertIn('exec "$LABGPU_REAL_CLAUDE" --settings "$LABGPU_CLAUDE_SETTINGS" "$@"', remote)
        self.assertIn('"$LABGPU_AI_TMPDIR/aiswitch"', remote)
        self.assertIn("__labgpu/session", remote)
        self.assertIn("Token: present (redacted)", remote)
        self.assertIn("App wrapper:", remote)
        self.assertIn('export PATH="$LABGPU_AI_TMPDIR:$PATH"', remote)
        self.assertIn('--rcfile "$LABGPU_AI_TMPDIR/bashrc"', remote)
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:27183", remote)
        self.assertIn(f"ANTHROPIC_API_KEY={SESSION_TOKEN}", remote)
        self.assertIn("CUDA_VISIBLE_DEVICES=0", remote)
        self.assertNotIn("0.0.0.0", " ".join(command.ssh_args))
        self.assertNotIn("sk-", " ".join(command.ssh_args))
        self.assertNotIn("SECRET", " ".join(command.ssh_args))
        self.assertNotIn("Authorization", " ".join(command.ssh_args))
        self.assertNotIn("Bearer", " ".join(command.ssh_args))
        self.assertNotIn("ANTHROPIC_API_KEY=sk-", " ".join(command.ssh_args))
        self.assertNotIn(SESSION_TOKEN, command.display_summary)

    def test_build_ai_ssh_command_for_codex_proxy_tunnel(self):
        command = build_ai_ssh_command(
            EnterServerAIRequest(
                server_alias="alpha_liu",
                gpu_index="auto",
                ai_app="codex",
                provider_name="OpenAI",
                ccswitch_proxy_port=15721,
                local_gateway_port=49231,
                remote_gateway_port=27183,
                session_token=SESSION_TOKEN,
                remote_cwd="/data/lsg/work/OPSD",
            )
        )

        self.assertEqual(command.ssh_args[:6], ["ssh", "-tt", "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:27183:127.0.0.1:49231"])
        remote = command.ssh_args[7]
        self.assertIn("LABGPU_AI_APP=codex", remote)
        self.assertIn("LABGPU_AI_PROVIDER=OpenAI", remote)
        self.assertIn("LABGPU_AI_BASE_URL=http://127.0.0.1:27183", remote)
        self.assertIn(f"LABGPU_AI_SESSION_TOKEN={SESSION_TOKEN}", remote)
        self.assertIn("OPENAI_BASE_URL=http://127.0.0.1:27183", remote)
        self.assertIn(f"OPENAI_API_KEY={SESSION_TOKEN}", remote)
        self.assertIn("LABGPU_CODEX_HOME", remote)
        self.assertIn("CODEX_HOME", remote)
        self.assertIn("auth.json", remote)
        self.assertIn("auth_mode", remote)
        self.assertIn("apikey", remote)
        self.assertIn("config.toml", remote)
        self.assertIn("openai_base_url", remote)
        self.assertIn('exec "$LABGPU_REAL_CODEX" "$@"', remote)
        self.assertIn("App wrapper:", remote)
        self.assertIn("cd /data/lsg/work/OPSD || exit 1", remote)
        self.assertNotIn("~/.codex", remote)
        self.assertNotIn("ANTHROPIC_BASE_URL=http://127.0.0.1:27183", remote)
        self.assertNotIn(f"ANTHROPIC_API_KEY={SESSION_TOKEN}", remote)
        self.assertNotIn("sk-", " ".join(command.ssh_args))
        self.assertNotIn("Authorization", " ".join(command.ssh_args))
        self.assertNotIn("Bearer", " ".join(command.ssh_args))
        self.assertIn("Codex CLI", command.display_summary)
        self.assertNotIn(SESSION_TOKEN, command.display_summary)

    def test_build_ai_ssh_command_quotes_provider_and_validates_gpu(self):
        command = build_ai_ssh_command(
            EnterServerAIRequest(
                server_alias="alpha_liu",
                gpu_index="0,1",
                ai_app="claude",
                provider_name="packy; touch /tmp/pwned",
                ccswitch_proxy_port=15721,
                local_gateway_port=49231,
                remote_gateway_port=27183,
                session_token=SESSION_TOKEN,
            )
        )
        remote = command.ssh_args[7]
        self.assertIn("LABGPU_AI_PROVIDER='packy; touch /tmp/pwned'", remote)
        self.assertIn("CUDA_VISIBLE_DEVICES=0,1", remote)

        with self.assertRaisesRegex(ValueError, "GPU index"):
            build_ai_ssh_command(
                EnterServerAIRequest(
                    server_alias="alpha_liu",
                    gpu_index="0; rm -rf ~",
                    ai_app="claude",
                    provider_name="PackyCode",
                    ccswitch_proxy_port=15721,
                    local_gateway_port=49231,
                    remote_gateway_port=27183,
                    session_token=SESSION_TOKEN,
                )
            )

    def test_build_ai_ssh_command_can_cd_to_remote_working_directory(self):
        command = build_ai_ssh_command(
            EnterServerAIRequest(
                server_alias="alpha_liu",
                gpu_index=None,
                ai_app="claude",
                provider_name="PackyCode",
                ccswitch_proxy_port=15721,
                local_gateway_port=49231,
                remote_gateway_port=27183,
                session_token=SESSION_TOKEN,
                remote_cwd="/data/lsg/work/OPSD",
            )
        )
        remote = command.ssh_args[7]
        self.assertIn("LABGPU_REMOTE_CWD=/data/lsg/work/OPSD", remote)
        self.assertIn("cd /data/lsg/work/OPSD || exit 1", remote)

        with self.assertRaisesRegex(ValueError, "working directory"):
            build_ai_ssh_command(
                EnterServerAIRequest(
                    server_alias="alpha_liu",
                    gpu_index=None,
                    ai_app="claude",
                    provider_name="PackyCode",
                    ccswitch_proxy_port=15721,
                    local_gateway_port=49231,
                    remote_gateway_port=27183,
                    session_token=SESSION_TOKEN,
                    remote_cwd="/data/lsg/work/OPSD; touch /tmp/pwned\n",
                )
            )

    def test_build_ai_ssh_command_supports_extra_path_and_claude_override(self):
        command = build_ai_ssh_command(
            EnterServerAIRequest(
                server_alias="alpha_liu",
                gpu_index=None,
                ai_app="claude",
                provider_name="PackyCode",
                ccswitch_proxy_port=15721,
                local_gateway_port=49231,
                remote_gateway_port=27183,
                session_token=SESSION_TOKEN,
                remote_path_prefixes=("~/miniconda3/bin", "/opt/claude/bin"),
                claude_command="~/miniconda3/bin/claude",
            )
        )
        remote = command.ssh_args[7]
        self.assertIn("LABGPU_AI_PATH_PREFIX='~/miniconda3/bin:/opt/claude/bin'", remote)
        self.assertIn("LABGPU_AI_CLAUDE_COMMAND='~/miniconda3/bin/claude'", remote)
        self.assertIn('LABGPU_REAL_CLAUDE="${LABGPU_AI_CLAUDE_COMMAND:-}"', remote)
        self.assertIn('LABGPU_REAL_CLAUDE="${HOME}/${LABGPU_REAL_CLAUDE#~/}"', remote)
        self.assertIn("PATH=${HOME}/miniconda3/bin:/opt/claude/bin:$PATH", remote)

    def test_claude_command_probe_uses_launch_path(self):
        script = build_claude_command_probe(remote_path_prefixes=("~/miniconda3/bin",), claude_command="~/miniconda3/bin/claude")
        self.assertIn("PATH=${HOME}/miniconda3/bin:$PATH", script)
        self.assertIn("bash -ic", script)
        self.assertIn("$HOME/miniconda3/bin/claude", script)
        self.assertIn("claude not found in LabGPU launch PATH", script)

    def test_build_ai_ssh_command_requires_provider_and_supported_app(self):
        with self.assertRaisesRegex(ValueError, "provider"):
            build_ai_ssh_command(
                EnterServerAIRequest(
                    server_alias="alpha_liu",
                    gpu_index=None,
                    ai_app="claude",
                    provider_name="",
                    ccswitch_proxy_port=15721,
                    local_gateway_port=49231,
                    remote_gateway_port=27183,
                    session_token=SESSION_TOKEN,
                )
            )
        with self.assertRaisesRegex(ValueError, "Only Claude Code and Codex CLI"):
            build_ai_ssh_command(
                EnterServerAIRequest(
                    server_alias="alpha_liu",
                    gpu_index=None,
                    ai_app="gemini",
                    provider_name="PackyCode",
                    ccswitch_proxy_port=15721,
                    local_gateway_port=49231,
                    remote_gateway_port=27183,
                    session_token=SESSION_TOKEN,
                )
            )

        with self.assertRaisesRegex(ValueError, "session token"):
            build_ai_ssh_command(
                EnterServerAIRequest(
                    server_alias="alpha_liu",
                    gpu_index=None,
                    ai_app="claude",
                    provider_name="PackyCode",
                    ccswitch_proxy_port=15721,
                    local_gateway_port=49231,
                    remote_gateway_port=27183,
                    session_token="sk-real-provider-key",
                )
            )


if __name__ == "__main__":
    unittest.main()
