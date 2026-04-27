from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from labgpu.core.events import append_event
from labgpu.core.models import RunMeta
from labgpu.core.paths import ensure_home, runs_dir
from labgpu.process.tree import process_tree_pids
from labgpu.utils.time import duration_seconds


class RunStore:
    def __init__(self, root: Path | None = None) -> None:
        ensure_home()
        self.root = Path(root).resolve() if root else runs_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def create(self, meta: RunMeta) -> RunMeta:
        run_dir = self.run_dir(meta.run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        self.write(meta)
        append_event(run_dir, "created", status=meta.status)
        return meta

    def create_run(self, meta: RunMeta) -> RunMeta:
        return self.create(meta)

    def write(self, meta: RunMeta) -> None:
        run_dir = self.run_dir(meta.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = meta.to_dict()
        path = run_dir / "meta.json"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=run_dir,
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            tmp_name = handle.name
        Path(tmp_name).replace(path)

    def update(self, run_id: str, **changes: Any) -> RunMeta:
        meta = self.get(run_id)
        data = meta.to_dict()
        data.update(changes)
        data["duration_seconds"] = duration_seconds(data.get("started_at"), data.get("ended_at"))
        updated = RunMeta.from_dict(data)
        self.write(updated)
        append_event(self.run_dir(run_id), "updated", changes=changes)
        return updated

    def update_run(self, run_id: str, **changes: Any) -> RunMeta:
        return self.update(run_id, **changes)

    def get(self, run_id: str) -> RunMeta:
        path = self.run_dir(run_id) / "meta.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunMeta.from_dict(data)

    def read_run(self, run_id: str) -> RunMeta:
        return self.get(run_id)

    def try_get(self, run_id: str) -> RunMeta | None:
        try:
            return self.get(run_id)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return None

    def list(self, *, all_runs: bool = False, user: str | None = None, status: str | None = None) -> list[RunMeta]:
        metas: list[RunMeta] = []
        for path in self.root.iterdir() if self.root.exists() else []:
            if not path.is_dir():
                continue
            meta = self.try_get(path.name)
            if not meta:
                continue
            if user and meta.user != user:
                continue
            if status and meta.status != status:
                continue
            if not all_runs and meta.status not in {"created", "running", "failed", "killed", "orphaned"}:
                continue
            metas.append(meta)
        metas.sort(
            key=lambda item: (
                item.status == "running",
                item.started_at or item.created_at or "",
            ),
            reverse=True,
        )
        return metas

    def list_runs(
        self,
        *,
        all_runs: bool = False,
        user: str | None = None,
        status: str | None = None,
    ) -> list[RunMeta]:
        return self.list(all_runs=all_runs, user=user, status=status)

    def resolve(self, ref: str) -> RunMeta | None:
        matches = self.resolve_all(ref)
        return matches[0] if matches else None

    def resolve_all(self, ref: str) -> list[RunMeta]:
        exact = self.try_get(ref)
        if exact:
            return [exact]
        return [
            meta
            for meta in self.list(all_runs=True)
            if meta.run_id.startswith(ref) or meta.name == ref
        ]

    def find_run_by_name_or_id(self, ref: str) -> RunMeta | None:
        return self.resolve(ref)

    def append_event(self, run_id: str, event_type: str, **payload: Any) -> None:
        append_event(self.run_dir(run_id), event_type, **payload)

    def running_by_pid(self, *, include_children: bool = True) -> dict[int, RunMeta]:
        out: dict[int, RunMeta] = {}
        for meta in self.list(all_runs=True, status="running"):
            if meta.pid is not None:
                out[int(meta.pid)] = meta
                if include_children:
                    for child in process_tree_pids(int(meta.pid))[1:]:
                        out[child] = meta
        return out
