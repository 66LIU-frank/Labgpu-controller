import tempfile
import unittest
from pathlib import Path

from labgpu.utils.git import git_metadata


class GitMetadataTest(unittest.TestCase):
    def test_non_git_directory_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = git_metadata(Path(tmp))
            self.assertIsNone(meta["git_commit"])
            self.assertFalse(meta["git_dirty"])


if __name__ == "__main__":
    unittest.main()
