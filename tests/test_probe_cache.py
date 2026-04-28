import os
import tempfile
import unittest

from labgpu.remote.cache import read_server_cache, write_server_cache


class ProbeCacheTest(unittest.TestCase):
    def test_write_and_read_server_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("LABGPU_HOME")
            os.environ["LABGPU_HOME"] = tmp
            try:
                payload = {"alias": "alpha/liu", "online": True, "gpus": [{"index": 0}], "probed_at": "now"}
                write_server_cache(payload)
                cached = read_server_cache("alpha/liu")
                self.assertEqual(cached["alias"], "alpha/liu")
                self.assertEqual(cached["gpus"][0]["index"], 0)
            finally:
                if old is None:
                    os.environ.pop("LABGPU_HOME", None)
                else:
                    os.environ["LABGPU_HOME"] = old


if __name__ == "__main__":
    unittest.main()
