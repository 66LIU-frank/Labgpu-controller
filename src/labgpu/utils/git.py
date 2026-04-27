from __future__ import annotations

import subprocess
from pathlib import Path


def git_metadata(cwd: Path) -> dict[str, object]:
    commit = _git(cwd, ["rev-parse", "--short", "HEAD"])
    branch = _git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
    remote = _git(cwd, ["config", "--get", "remote.origin.url"])
    dirty = bool(_git(cwd, ["status", "--porcelain"]))
    return {
        "git_commit": commit or None,
        "git_branch": branch or None,
        "git_remote": remote or None,
        "git_dirty": dirty,
    }


def write_git_patch(cwd: Path, target: Path) -> str | None:
    diff = _git(cwd, ["diff", "--binary"])
    if not diff:
        return None
    target.write_text(diff, encoding="utf-8", errors="replace")
    return str(target)


def _git(cwd: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
