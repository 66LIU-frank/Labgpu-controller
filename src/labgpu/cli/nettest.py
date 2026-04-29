from __future__ import annotations

from argparse import Namespace

from labgpu.remote.transfer import dumps_json, planned_nettests, run_nettests


def run(args: Namespace) -> int:
    if args.plan:
        tests = planned_nettests(args.source, args.target, mb=args.mb, both=args.both, direct=args.direct)
        if args.json:
            print(dumps_json({"tests": tests, "note": relay_note(args.direct)}))
            return 0
        print("Planned network tests")
        for test in tests:
            print(f"  - {test}")
        print(relay_note(args.direct))
        return 0

    results = run_nettests(args.source, args.target, mb=args.mb, timeout=args.timeout, both=args.both, direct=args.direct)
    if args.json:
        print(dumps_json({"results": [item.as_dict() for item in results], "note": relay_note(args.direct)}))
        return 0 if all(item.ok for item in results) else 1

    print("Network test")
    print(relay_note(args.direct))
    print(f"{'Direction':42} {'Status':8} {'MiB/s':>10} {'Time':>8}")
    print("-" * 74)
    for item in results:
        status = "ok" if item.ok else "failed"
        print(f"{item.direction[:42]:42} {status:8} {item.mb_per_second:10.2f} {item.seconds:8.2f}s")
        if item.message:
            print(f"  {item.message}")
    return 0 if all(item.ok for item in results) else 1


def relay_note(direct: bool) -> str:
    if direct:
        return "Includes direct SSH tests. Direct mode requires the source server to SSH into the target server."
    return "Default relay mode measures LabGPU project-copy speed through this laptop, not raw server-to-server LAN speed."
