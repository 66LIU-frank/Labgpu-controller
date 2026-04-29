from __future__ import annotations

from argparse import Namespace

from labgpu.remote.transfer import build_transfer_plan, dumps_json, run_transfer_plan


def run(args: Namespace) -> int:
    plan = build_transfer_plan(
        args.source,
        args.target,
        excludes=list(args.exclude or []),
        no_default_excludes=bool(args.no_default_excludes),
    )
    if args.json:
        payload = {"execute": bool(args.execute), "plan": plan.as_dict()}
        if args.execute:
            if not args.yes and not confirm_overwrite(plan.target.host, plan.target.path):
                payload["cancelled"] = True
                print(dumps_json(payload))
                return 2
            payload["result"] = run_transfer_plan(plan, timeout=args.timeout)
            print(dumps_json(payload))
            return 0 if payload["result"].get("ok") else 1
        print(dumps_json(payload))
        return 0

    print("Project transfer plan")
    print(f"  from: {plan.source.host}:{plan.source.path}")
    print(f"  to:   {plan.target.host}:{plan.target.path}")
    if plan.excludes:
        print(f"  excludes: {', '.join(plan.excludes)}")
    print()
    print("Copyable pipeline:")
    print(f"  {plan.as_dict()['copyable_pipeline']}")
    if not args.execute:
        print()
        print("Dry run only. Add --execute to stream the project through this laptop.")
        return 0
    if not args.yes and not confirm_overwrite(plan.target.host, plan.target.path):
        print("Cancelled.")
        return 2
    result = run_transfer_plan(plan, timeout=args.timeout)
    if result.get("ok"):
        print(f"Transferred {format_bytes(int(result['bytes']))} in {result['seconds']}s ({result['mb_per_second']} MiB/s).")
        return 0
    print(f"Transfer failed: {result.get('message') or 'unknown error'}")
    return 1


def confirm_overwrite(host: str, path: str) -> bool:
    answer = input(f"This may overwrite files under {host}:{path}. Continue? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def format_bytes(value: int) -> str:
    if value >= 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024 * 1024):.1f} GiB"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} B"
