from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DiagnosisRule:
    type: str
    title: str
    pattern: re.Pattern[str]
    suggestion: str


RULES: list[DiagnosisRule] = [
    DiagnosisRule(
        "cuda_oom",
        "CUDA out of memory",
        re.compile(r"cuda out of memory|OutOfMemoryError|failed to allocate.*cuda", re.I),
        "Reduce batch size, enable gradient accumulation, or check whether another process is using the GPU.",
    ),
    DiagnosisRule(
        "loss_nan",
        "NaN or Inf detected",
        re.compile(r"loss[:= ]+nan|(^|[^a-z])(nan|inf|infinity)([^a-z]|$)", re.I),
        "Check learning rate, input data, loss scaling, and mixed precision settings.",
    ),
    DiagnosisRule(
        "traceback",
        "Python traceback",
        re.compile(r"Traceback \(most recent call last\):", re.I),
        "Inspect the final exception in the traceback.",
    ),
    DiagnosisRule(
        "killed",
        "Process was killed",
        re.compile(r"(^|\s)(Killed|SIGKILL|oom-kill|out of memory: killed process)(\s|$)", re.I),
        "The process may have hit system memory limits or been terminated by a user or administrator.",
    ),
    DiagnosisRule(
        "disk_full",
        "No space left on device",
        re.compile(r"No space left on device|Disk quota exceeded", re.I),
        "Clean checkpoints, logs, dataset cache, or move outputs to a larger disk.",
    ),
    DiagnosisRule(
        "module_not_found",
        "Missing Python module",
        re.compile(r"ModuleNotFoundError: No module named ['\"]?([^'\"]+)", re.I),
        "Check the active conda/venv environment and install the missing package.",
    ),
    DiagnosisRule(
        "import_error",
        "Import error",
        re.compile(r"ImportError:", re.I),
        "Check package versions and Python environment compatibility.",
    ),
    DiagnosisRule(
        "permission_denied",
        "Permission denied",
        re.compile(r"Permission denied|Operation not permitted", re.I),
        "Check file, directory, and device permissions.",
    ),
    DiagnosisRule(
        "port_in_use",
        "Address already in use",
        re.compile(r"Address already in use|port .* already in use", re.I),
        "Use a different port or stop the process that owns the current port.",
    ),
    DiagnosisRule(
        "nccl_error",
        "NCCL error",
        re.compile(r"NCCL error|unhandled system error", re.I),
        "Check multi-GPU communication, CUDA/NCCL versions, network interfaces, and visible devices.",
    ),
]
