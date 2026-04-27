# Contributing

LabGPU should stay small, testable, and useful on real shared GPU servers.

Rules for changes:

- Keep CLI workflows simple.
- Do not introduce scheduling, quotas, reservation calendars, Docker orchestration, Kubernetes, or Slurm replacement behavior without a design discussion.
- Keep tests runnable without NVIDIA GPUs.
- Add fake collectors or mocks for hardware-dependent behavior.
- Treat `meta.json`, `events.jsonl`, and logs in the run directory as durable user data.
- Degrade gracefully when process details are hidden by permissions.

Run tests with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
