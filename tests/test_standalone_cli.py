import unittest
from unittest.mock import patch

from labgpu.cli import standalone


class StandaloneCliTest(unittest.TestCase):
    def test_no_args_launches_desktop(self):
        with patch("labgpu.cli.standalone.cli_main", return_value=0) as cli_main:
            self.assertEqual(standalone.main([]), 0)
        cli_main.assert_called_once_with(["desktop"])

    def test_args_forward_to_labgpu_cli(self):
        with patch("labgpu.cli.standalone.cli_main", return_value=0) as cli_main:
            self.assertEqual(standalone.main(["doctor"]), 0)
        cli_main.assert_called_once_with(["doctor"])


if __name__ == "__main__":
    unittest.main()
