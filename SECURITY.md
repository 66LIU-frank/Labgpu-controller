# Security Policy

LabGPU is designed as a personal local tool for shared SSH GPU servers.

- Keep `labgpu ui` bound to `127.0.0.1` unless you explicitly accept the risk.
- Do not expose LabGPU Home directly on a public network.
- Agentless stop actions are only intended for processes owned by the current SSH user.
- Configure `shared_account = true` for shared Linux accounts so stop actions are disabled by default.
- `labgpu context` redacts common secret-looking environment names by default.

For more detail, see [docs/security.md](docs/security.md).

Please report security-sensitive issues privately if possible, and avoid posting secrets, hostnames, usernames, private paths, or full commands in public issues.
