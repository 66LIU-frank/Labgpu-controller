import unittest

from labgpu.runner.base import make_run_id


class RunIdTest(unittest.TestCase):
    def test_run_id_slugifies_name(self):
        run_id = make_run_id("My Experiment 001")
        self.assertTrue(run_id.startswith("my-experiment-001-"))
        self.assertNotIn(" ", run_id)


if __name__ == "__main__":
    unittest.main()
