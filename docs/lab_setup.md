# Lab Setup

Personal mode stores data in:

```text
~/.labgpu
```

Shared lab mode stores data in a common directory:

```bash
mkdir -p /shared/labgpu
chmod 1777 /shared/labgpu
export LABGPU_HOME=/shared/labgpu
```

Add the export to each user's shell profile if the lab wants one shared dashboard.

The Web dashboard has no login system in the MVP. Keep it bound to localhost and access it with SSH tunneling:

```bash
labgpu web
ssh -L 8765:localhost:8765 user@gpu-server
```
