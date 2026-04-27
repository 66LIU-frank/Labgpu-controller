from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path
from typing import Any


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def inspect_process(pid: int) -> dict[str, Any]:
    info: dict[str, Any] = {"pid": pid, "permission_error": False}
    info.update(_inspect_psutil(pid))
    proc_dir = Path("/proc") / str(pid)
    if proc_dir.exists():
        info.update({key: value for key, value in _inspect_procfs(proc_dir).items() if value is not None and not info.get(key)})
    info.update({key: value for key, value in _inspect_ps(pid).items() if value and not info.get(key)})
    if info.get("user") and not info.get("username"):
        info["username"] = info["user"]
    if info.get("command") and not info.get("cmdline"):
        info["cmdline"] = info["command"]
    if info.get("command_error") or info.get("cwd_error") or info.get("status_error"):
        info["permission_error"] = True
    return info


def _inspect_psutil(pid: int) -> dict[str, Any]:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return {}
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline())
        return {
            "username": proc.username(),
            "user": proc.username(),
            "cmdline": cmdline,
            "command": cmdline,
            "cwd": proc.cwd(),
            "create_time": proc.create_time(),
            "permission_error": False,
        }
    except psutil.AccessDenied:
        return {"permission_error": True}
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return {"missing": True}


def _inspect_procfs(proc_dir: Path) -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        for line in (proc_dir / "status").read_text(errors="replace").splitlines():
            if line.startswith("Uid:"):
                uid = int(line.split()[1])
                info["uid"] = uid
                try:
                    info["user"] = pwd.getpwuid(uid).pw_name
                    info["username"] = info["user"]
                except KeyError:
                    info["user"] = str(uid)
                    info["username"] = info["user"]
            elif line.startswith("Name:"):
                info["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Starttime:"):
                info["start_time_ticks"] = line.split(":", 1)[1].strip()
    except (OSError, ValueError):
        info["status_error"] = "permission_denied"

    try:
        raw = (proc_dir / "cmdline").read_bytes()
        command = " ".join(part.decode(errors="replace") for part in raw.split(b"\0") if part)
        if command:
            info["command"] = command
            info["cmdline"] = command
    except OSError:
        info["command_error"] = "permission_denied"

    try:
        info["cwd"] = str((proc_dir / "cwd").readlink())
    except OSError:
        info["cwd_error"] = "permission_denied"

    return info


def _inspect_ps(pid: int) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "user=,lstart=,command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    line = result.stdout.strip().splitlines()[0]
    parts = line.split(maxsplit=6)
    if not parts:
        return {}
    user = parts[0]
    command = parts[6] if len(parts) >= 7 else ""
    create_time = " ".join(parts[1:6]) if len(parts) >= 6 else None
    return {
        "user": user,
        "username": user,
        "command": command,
        "cmdline": command,
        "create_time": create_time,
    }
