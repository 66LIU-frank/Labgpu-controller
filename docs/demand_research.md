# Demand Research Plan

LabGPU does not need research to prove that nobody has built adjacent tools. Adjacent tools clearly exist: GPU process monitors, Web dashboards, lab reservation systems, single-node schedulers, and MLOps trackers.

The research goal is narrower:

```text
Do student users of shared SSH GPU servers need a personal training workspace that helps them find GPUs, launch/adopt runs, recover their own tasks, save reproducibility context, diagnose failures, and export AI/debug context?
```

## Interview Targets

- 3-5 deep learning students who run training jobs daily.
- 2-3 students who use more than one SSH GPU host.
- 1-2 users outside the immediate lab who also use shared GPU machines without admin rights.

## Interview Questions

- How often do you run `nvidia-smi`, `gpustat`, or `nvitop`?
- How do you decide which GPU is free?
- When a GPU is occupied, can you tell which experiment it is?
- Do you use tmux, screen, nohup, Slurm, or something else?
- Where do logs usually go?
- How long does it take to notice a failed overnight run?
- What are the most common failures: OOM, NaN, NCCL, disk full, missing package, permission issue, or port conflict?
- Have you tried MLflow, W&B, ClearML, or similar tools? Why did you keep or abandon them?
- Would you change `python train.py` into `labgpu run --name exp --gpu 0 -- python train.py`?
- Would `labgpu pick --min-vram 24G --prefer A100` be faster than manual SSH hopping?
- Would `labgpu adopt <pid> --name exp` help with existing tmux/nohup jobs?
- What should be hidden in a personal workspace running on shared servers: cwd, full command, env, username, or log path?

## Alpha Trial

Ask three users to run:

```bash
labgpu doctor
labgpu status
labgpu run --name test --gpu 0 -- python train.py
labgpu logs test
labgpu diagnose test
labgpu context test
labgpu ui
labgpu pick
labgpu where
```

Record:

- installation failures
- status accuracy
- whether log capture worked
- whether diagnosis was useful
- whether the personal UI helped them start faster
- whether `where` helped recover a forgotten run
- whether the user would keep using it

## One-Week Run Study

Use LabGPU for 20-50 real runs and count:

- success / failed / killed runs
- failure reason distribution
- OOM frequency
- NaN/NCCL/environment/disk failures
- untracked process count
- adopted process count
- how often users use `context` or `report`

The strongest evidence will come from real run folders, not survey answers.
