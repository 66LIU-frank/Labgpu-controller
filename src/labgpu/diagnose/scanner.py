from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from labgpu.diagnose.rules import RULES


def scan_text(text: str) -> dict[str, Any]:
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        for rule in RULES:
            if rule.pattern.search(stripped):
                return {
                    "type": rule.type,
                    "title": rule.title,
                    "severity": "error",
                    "evidence": f"line {line_number}: {stripped[:500]}",
                    "line_number": line_number,
                    "suggestion": rule.suggestion,
                }
    return {
        "type": "unknown",
        "title": "Unknown",
        "severity": "info",
        "evidence": None,
        "line_number": None,
        "suggestion": "Inspect the log tail and exit code manually.",
    }


def scan_log(path: str | Path, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {
            "type": "unknown",
            "title": "Log not found",
            "severity": "warning",
            "evidence": None,
            "line_number": None,
            "suggestion": "Check whether the run has a log_path.",
        }
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
        text = handle.read().decode(errors="replace")
    return scan_text(text)


def write_diagnosis(run_dir: Path, diagnosis: dict[str, Any]) -> Path:
    path = run_dir / "diagnosis.json"
    path.write_text(json.dumps(diagnosis, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path
