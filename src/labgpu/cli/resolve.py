from __future__ import annotations

import sys

from labgpu.core.models import RunMeta
from labgpu.core.store import RunStore


def resolve_run(store: RunStore, ref: str) -> RunMeta:
    matches = store.resolve_all(ref)
    if not matches:
        raise RuntimeError(f"unknown experiment: {ref}")
    if len(matches) > 1:
        print(
            f"labgpu: matched {len(matches)} runs for {ref!r}; using latest {matches[0].run_id}",
            file=sys.stderr,
        )
    return matches[0]


def resolve_run_strict(store: RunStore, ref: str, *, action: str) -> RunMeta:
    matches = store.resolve_all(ref)
    if not matches:
        raise RuntimeError(f"unknown experiment: {ref}")
    if len(matches) > 1:
        lines = "\n".join(f"- {meta.run_id} ({meta.status})" for meta in matches[:10])
        raise RuntimeError(
            f"multiple runs matched {ref!r}; refusing to {action}. "
            f"Use a full run_id.\n{lines}"
        )
    return matches[0]
