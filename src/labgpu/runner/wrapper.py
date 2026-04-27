from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

from labgpu.core.events import append_event
from labgpu.core.store import RunStore
from labgpu.diagnose.scanner import scan_log, write_diagnosis
from labgpu.utils.time import now_utc


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m labgpu.runner.wrapper RUN_ID", file=sys.stderr)
        return 2
    run_id = argv[0]
    store = RunStore()
    meta = store.get(run_id)
    run_dir = store.run_dir(run_id)
    log_path = Path(meta.log_path or run_dir / "stdout.log")
    env = os.environ.copy()
    env.update(meta.env)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        try:
            return _run(meta.command_argv, meta.cwd, env, store, run_id, run_dir, log)
        except Exception:  # noqa: BLE001 - wrapper must persist unexpected failures.
            log.write("\n[labgpu] wrapper crashed\n")
            log.write(traceback.format_exc())
            diagnosis = {
                "type": "wrapper_error",
                "title": "LabGPU wrapper error",
                "severity": "error",
                "evidence": None,
                "line_number": None,
                "suggestion": "Check command.sh and wrapper log output.",
            }
            write_diagnosis(run_dir, diagnosis)
            store.update(
                run_id,
                status="failed",
                ended_at=now_utc(),
                failure_reason=diagnosis["title"],
                failure_evidence=diagnosis["evidence"],
            )
            append_event(run_dir, "failed", reason=diagnosis["title"])
            return 1


def _run(
    command: list[str],
    cwd: str,
    env: dict[str, str],
    store: RunStore,
    run_id: str,
    run_dir: Path,
    log,
) -> int:
    log.write(f"[labgpu] run_id={run_id}\n")
    log.write(f"[labgpu] cwd={cwd}\n")
    log.write(f"[labgpu] started_at={now_utc()}\n\n")
    log.flush()
    started = now_utc()
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    store.update(run_id, status="running", started_at=started, pid=proc.pid, process_group_id=proc.pid)
    append_event(run_dir, "started", pid=proc.pid)
    assert proc.stdout is not None
    for line in proc.stdout:
        log.write(line)
        log.flush()
    exit_code = proc.wait()
    ended = now_utc()
    diagnosis = scan_log(log.name)
    write_diagnosis(run_dir, diagnosis)
    status = "success" if exit_code == 0 else "failed"
    reason = None if status == "success" and diagnosis["type"] == "unknown" else diagnosis["title"]
    evidence = diagnosis.get("evidence")
    store.update(
        run_id,
        status=status,
        ended_at=ended,
        exit_code=exit_code,
        failure_reason=reason,
        failure_evidence=evidence,
    )
    append_event(run_dir, "finished", exit_code=exit_code, status=status)
    log.write(f"\n[labgpu] ended_at={ended} exit_code={exit_code}\n")
    if reason:
        log.write(f"[labgpu] diagnosis={reason}\n")
    log.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
