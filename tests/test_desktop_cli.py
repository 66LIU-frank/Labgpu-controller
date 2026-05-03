import argparse
import unittest
from unittest.mock import patch

from labgpu.cli import desktop
from labgpu.cli.main import build_parser


class DesktopCliTest(unittest.TestCase):
    def test_desktop_command_is_registered(self):
        args = build_parser().parse_args(["desktop", "--port", "8899", "--no-open", "--hosts", "alpha_liu"])
        self.assertEqual(args.port, 8899)
        self.assertEqual(args.hosts, "alpha_liu")
        self.assertIs(args.handler, desktop.run)

    def test_run_uses_free_port_when_port_is_zero(self):
        args = argparse.Namespace(
            host="127.0.0.1",
            port=0,
            no_open=True,
            browser="auto",
            config=None,
            hosts="alpha_liu,beta",
            pattern=None,
            timeout=3,
            allow_actions=False,
            fake_lab=False,
        )
        with patch("labgpu.cli.desktop.find_free_port", return_value=45678), patch("labgpu.cli.desktop.serve") as serve:
            code = desktop.run(args)
        self.assertEqual(code, 0)
        serve.assert_called_once()
        kwargs = serve.call_args.kwargs
        self.assertEqual(kwargs["port"], 45678)
        self.assertEqual(kwargs["names"], ["alpha_liu", "beta"])
        self.assertFalse(kwargs["open_browser"])

    def test_macos_app_window_uses_browser_app_mode(self):
        class Result:
            returncode = 0

        with patch("labgpu.cli.desktop.subprocess.run", return_value=Result()) as run:
            self.assertTrue(desktop.open_macos_app_window("http://127.0.0.1:8798", "Microsoft Edge"))
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["/usr/bin/open", "-na", "Microsoft Edge", "--args"])
        self.assertIn("--app=http://127.0.0.1:8798", argv)

    def test_open_desktop_window_falls_back_to_webbrowser(self):
        with patch("labgpu.cli.desktop.platform.system", return_value="Linux"), patch("labgpu.cli.desktop.webbrowser.open", return_value=True) as open_browser:
            self.assertTrue(desktop.open_desktop_window("http://127.0.0.1:8798"))
        open_browser.assert_called_once_with("http://127.0.0.1:8798")


if __name__ == "__main__":
    unittest.main()
