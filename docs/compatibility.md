# Compatibility

LabGPU is alpha software. This page documents what the current agentless SSH workflow is designed to handle and where validation is still needed.

| Environment | Status | Notes |
| --- | --- | --- |
| NVIDIA GPUs with `nvidia-smi` | Supported | Primary target. LabGPU reads GPU memory, utilization, temperature, and compute processes over SSH. |
| Multi-host SSH aliases | Supported | Uses your local `~/.ssh/config`; save common hosts in Settings or `labgpu servers import-ssh`. |
| MIG | Partial | Basic `nvidia-smi` output may work, but MIG instance mapping needs more real-server validation. |
| Docker containers | Partial | Processes may appear as host PIDs; command, cwd, and user information can be hidden by permissions or container boundaries. |
| MPS | Partial | GPU ownership and utilization attribution may be ambiguous. Treat health labels as advisory. |
| Slurm environments | Read-only | LabGPU can inspect SSH hosts but does not submit, schedule, reserve, cancel, or manage Slurm jobs. |
| Shared Linux accounts | Limited | Agentless ownership is ambiguous. Configure `shared_account = true` to disable stop actions by default. |
| LabGPU Enhanced Mode | Optional | If a remote `labgpu` command is available, LabGPU can show tracked runs and richer context; otherwise it falls back to Agentless Mode. |
| AMD/ROCm | Not supported | Current GPU collection targets NVIDIA `nvidia-smi`. |
| Apple/Intel GPU | Not a target | LabGPU is focused on remote training servers. |

## Reporting Results

Use the real server validation issue template when trying LabGPU on a new lab/server setup. Include GPU model, driver version, whether Docker/MIG/MPS/Slurm is involved, and which commands worked.

Redact private hostnames, usernames, paths, project names, commands, logs, and tokens before posting.
