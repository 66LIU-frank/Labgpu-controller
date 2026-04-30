import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from labgpu.remote.vscode_recent import parse_vscode_remote_folder_uri, read_vscode_recent_remote_folders


class VSCodeRecentFoldersTest(unittest.TestCase):
    def test_parses_remote_ssh_folder_uri(self):
        folder = parse_vscode_remote_folder_uri(
            "vscode-remote://ssh-remote%2Balpha_liu/data/lsg/work/OPSD",
            label="/data/lsg/work/OPSD [SSH: alpha_liu]",
            source="test",
        )

        self.assertIsNotNone(folder)
        assert folder is not None
        self.assertEqual(folder.server_alias, "alpha_liu")
        self.assertEqual(folder.path, "/data/lsg/work/OPSD")
        self.assertEqual(folder.label, "/data/lsg/work/OPSD [SSH: alpha_liu]")

    def test_reads_recent_remote_folders_from_vscode_state_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp) / "Code" / "User"
            state_db = user_dir / "globalStorage" / "state.vscdb"
            state_db.parent.mkdir(parents=True)
            conn = sqlite3.connect(state_db)
            try:
                conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
                conn.execute(
                    "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                    (
                        "history.recentlyOpenedPathsList",
                        json.dumps(
                            {
                                "entries": [
                                    {
                                        "folderUri": "vscode-remote://ssh-remote%2Balpha_liu/data/lsg/work/OPSD",
                                        "label": "/data/lsg/work/OPSD [SSH: alpha_liu]",
                                        "remoteAuthority": "ssh-remote+alpha_liu",
                                    },
                                    {"folderUri": "file:///Users/me/local-project"},
                                ]
                            }
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            folders = read_vscode_recent_remote_folders(user_dirs=[user_dir])

        self.assertEqual(
            folders,
            [
                {
                    "server_alias": "alpha_liu",
                    "path": "/data/lsg/work/OPSD",
                    "label": "/data/lsg/work/OPSD [SSH: alpha_liu]",
                    "source": "vscode-state",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
