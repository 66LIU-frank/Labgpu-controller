import io
import json
import unittest
from argparse import Namespace
from contextlib import redirect_stdout

from labgpu.cli import nettest, sync
from labgpu.cli.main import build_parser
from labgpu.remote.transfer import build_transfer_plan, parse_remote_path, planned_nettests


class TransferToolsTest(unittest.TestCase):
    def test_parse_remote_path(self):
        remote = parse_remote_path("alpha_liu:/data/me/project")
        self.assertEqual(remote.host, "alpha_liu")
        self.assertEqual(remote.path, "/data/me/project")
        with self.assertRaises(ValueError):
            parse_remote_path("-bad:/tmp/project")
        with self.assertRaises(ValueError):
            parse_remote_path("alpha_liu")

    def test_transfer_plan_is_copyable_and_excludes_caches(self):
        plan = build_transfer_plan("alpha_liu:/src/project", "alpha_shi:/dst/project", excludes=["data"])
        payload = plan.as_dict()
        self.assertIn("ssh alpha_liu", payload["copyable_pipeline"])
        self.assertIn("ssh alpha_shi", payload["copyable_pipeline"])
        self.assertIn("--exclude=__pycache__", payload["source_command"])
        self.assertIn("--exclude=data", payload["source_command"])
        self.assertIn("tar -xf - -C /dst/project", payload["target_command"])

    def test_sync_cli_defaults_to_dry_run(self):
        args = Namespace(
            source="alpha_liu:/src/project",
            target="alpha_shi:/dst/project",
            exclude=[],
            no_default_excludes=False,
            execute=False,
            yes=False,
            timeout=10,
            json=True,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = sync.run(args)
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["execute"])
        self.assertIn("copyable_pipeline", payload["plan"])

    def test_nettest_plan_lists_relay_and_direct_modes(self):
        tests = planned_nettests("alpha_liu", "alpha_shi", mb=16, both=True, direct=True)
        self.assertIn("alpha_liu -> alpha_shi via local relay (16 MiB)", tests)
        self.assertIn("alpha_shi -> alpha_liu via local relay (16 MiB)", tests)
        self.assertIn("alpha_liu -> alpha_shi direct ssh (16 MiB)", tests)

    def test_nettest_cli_plan_does_not_run_ssh(self):
        args = Namespace(source="alpha_liu", target="alpha_shi", mb=8, timeout=10, both=False, direct=False, plan=True, json=True)
        output = io.StringIO()
        with redirect_stdout(output):
            code = nettest.run(args)
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertIn("alpha_liu -> alpha_shi via local relay (8 MiB)", payload["tests"])

    def test_parser_accepts_sync_and_nettest(self):
        parser = build_parser()
        sync_args = parser.parse_args(["sync", "alpha_liu:/src", "alpha_shi:/dst"])
        self.assertEqual(sync_args.command, "sync")
        self.assertFalse(sync_args.execute)
        speed_args = parser.parse_args(["speed", "alpha_liu", "alpha_shi", "--plan"])
        self.assertEqual(speed_args.command, "speed")
        self.assertTrue(speed_args.plan)
