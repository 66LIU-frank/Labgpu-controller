# Lab Setup

Personal mode stores data in:

```text
~/.labgpu
```

Most users should keep the default personal `~/.labgpu`. Shared `LABGPU_HOME` is advanced/experimental because it can expose run metadata, commands, paths, and logs to other users.

If a group deliberately wants a shared run registry, use a group-owned directory:

```bash
sudo groupadd labgpu
sudo usermod -aG labgpu alice
sudo usermod -aG labgpu bob
sudo mkdir -p /shared/labgpu
sudo chgrp labgpu /shared/labgpu
sudo chmod 2770 /shared/labgpu
export LABGPU_HOME=/shared/labgpu
```

The setgid bit (`2` in `2770`) keeps new files owned by the `labgpu` group. Avoid
world-writable shared directories such as `chmod 1777 /shared/labgpu`; they make
it too easy for users to overwrite or inspect each other's run metadata.

Add the export to each user's shell profile if the lab wants one shared run
registry:

```bash
echo 'export LABGPU_HOME=/shared/labgpu' >> ~/.bashrc
```

Each user may need to log out and back in after being added to the group.

For the local personal workspace, users normally do not need a shared remote
directory at all. They can run:

```bash
labgpu ui
```

from their laptop and import SSH hosts through the Settings page.

If the lab later enables Enhanced Mode on shared servers, keep the shared
directory group-scoped and review `docs/security.md`.

Do not make that directory world-writable in real lab use.

The Web dashboard has no login system in the MVP. Keep it bound to localhost and access it with SSH tunneling:

```bash
labgpu web
ssh -L 8765:localhost:8765 user@gpu-server
```
