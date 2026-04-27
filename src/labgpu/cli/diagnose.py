from __future__ import annotations

from pathlib import Path

from labgpu.cli.resolve import resolve_run
from labgpu.core.store import RunStore
from labgpu.diagnose.scanner import scan_log, write_diagnosis


def run(args) -> int:
    store = RunStore()
    meta = resolve_run(store, args.ref)
    if not meta.log_path:
        raise RuntimeError("this run has no associated log file")
    diagnosis = scan_log(meta.log_path)
    write_diagnosis(store.run_dir(meta.run_id), diagnosis)
    if diagnosis["type"] != "unknown":
        store.update(
            meta.run_id,
            failure_reason=diagnosis["title"],
            failure_evidence=diagnosis.get("evidence"),
        )
    print(f"Experiment: {meta.name}")
    print(f"Status: {meta.status}")
    print(f"Exit code: {meta.exit_code if meta.exit_code is not None else '-'}")
    print(f"Detected: {diagnosis['title']}")
    if diagnosis.get("evidence"):
        print(f"Evidence: {diagnosis['evidence']}")
    print(f"Suggestion: {diagnosis['suggestion']}")
    return 0
