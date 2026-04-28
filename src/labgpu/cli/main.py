from __future__ import annotations

import argparse
import sys

from labgpu import __version__
from labgpu.cli import adopt, context as context_cmd, demo, diagnose, doctor, kill, list as list_cmd, logs, pick, refresh, report, run as run_cmd, servers, status, ui, web, where


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    try:
        return int(args.handler(args) or 0)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - concise CLI errors.
        print(f"labgpu: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labgpu",
        description="Personal GPU workspace for students using shared SSH servers.",
    )
    parser.add_argument("--version", action="version", version=f"labgpu {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("doctor", help="check local LabGPU environment")
    p.set_defaults(handler=doctor.run)

    p = sub.add_parser("status", help="show GPU status")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fake", action="store_true", help="use fake GPU data for demos and tests")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--interval", type=float, default=3.0)
    p.set_defaults(handler=status.run)

    p = sub.add_parser("pick", help="recommend a GPU across SSH-configured hosts")
    p.add_argument("--hosts", help="comma-separated SSH aliases, for example alpha_liu,Song-1")
    p.add_argument("--pattern", help="filter SSH aliases by substring")
    p.add_argument("--config", help="SSH config path, default ~/.ssh/config")
    p.add_argument("--timeout", type=int, default=8)
    p.add_argument("--prefer", "--model", dest="prefer", help="preferred GPU model, for example A100, 4090, or H800")
    p.add_argument("--tag", help="server tag filter")
    p.add_argument("--min-vram", help="minimum free VRAM, for example 24G or 40960M")
    p.add_argument("--min-free-gb", type=float, default=0, help=argparse.SUPPRESS)
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--all", action="store_true", help="include busy and not-recommended GPUs")
    p.add_argument("--json", action="store_true")
    p.add_argument("--cmd", action="store_true", help="print copyable launch snippets only")
    p.add_argument("--fake-lab", action="store_true", help="use built-in multi-server demo data")
    p.set_defaults(handler=pick.run)

    p = sub.add_parser("where", help="show where your training is running")
    p.add_argument("--hosts", help="comma-separated SSH aliases, for example alpha_liu,Song-1")
    p.add_argument("--pattern", help="filter SSH aliases by substring")
    p.add_argument("--config", help="SSH config path, default ~/.ssh/config")
    p.add_argument("--timeout", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p.add_argument("--fake-lab", action="store_true", help="use built-in multi-server demo data")
    p.set_defaults(handler=where.run)

    p = sub.add_parser("refresh", help="reconcile running runs with current processes")
    p.set_defaults(handler=refresh.run)

    p = sub.add_parser("run", help="launch an experiment in tmux")
    p.add_argument("--name", required=True)
    p.add_argument("--gpu", help="CUDA_VISIBLE_DEVICES value, for example 0 or 0,1")
    p.add_argument("--project")
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--note")
    p.add_argument("--config", action="append", default=[])
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(handler=run_cmd.run)

    p = sub.add_parser("list", help="list experiments")
    p.add_argument("--all", action="store_true")
    p.add_argument("--user")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=list_cmd.run)

    p = sub.add_parser("logs", help="show logs")
    p.add_argument("ref")
    p.add_argument("--tail", type=int, default=100)
    p.add_argument("--follow", action="store_true")
    p.set_defaults(handler=logs.run)

    p = sub.add_parser("kill", help="terminate your own experiment")
    p.add_argument("ref")
    p.add_argument("--force", action="store_true")
    p.set_defaults(handler=kill.run)

    p = sub.add_parser("diagnose", help="diagnose a failed run from logs")
    p.add_argument("ref")
    p.set_defaults(handler=diagnose.run)

    p = sub.add_parser("report", help="print a Markdown experiment report")
    p.add_argument("ref")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=report.run)

    p = sub.add_parser("context", help="export AI/teammate debug context for a run")
    p.add_argument("ref")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--tail", type=int, default=200)
    p.add_argument("--max-bytes", type=int, default=200_000)
    p.add_argument("--include-env", action="store_true", help="include all recorded env fields after redaction")
    p.add_argument("--no-redact", dest="redact", action="store_false", help="disable redaction; may expose secrets")
    p.add_argument("--copy", action="store_true", help="copy Markdown or JSON context to the clipboard")
    p.set_defaults(redact=True)
    p.set_defaults(handler=context_cmd.run)

    p = sub.add_parser("adopt", help="record an existing PID as a LabGPU run")
    p.add_argument("pid", type=int)
    p.add_argument("--name", required=True)
    p.add_argument("--gpu")
    p.add_argument("--log")
    p.add_argument("--project")
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--note")
    p.set_defaults(handler=adopt.run)

    p = sub.add_parser("web", help="start the older single-machine run UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--fake", action="store_true")
    p.set_defaults(handler=web.run)

    p = sub.add_parser("demo", help="start LabGPU Home with built-in fake multi-server data")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--timeout", type=int, default=8)
    p.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    p.set_defaults(handler=demo.run)

    p = sub.add_parser("ui", help="start LabGPU Home for SSH-configured servers")
    p.add_argument("--hosts", help="comma-separated SSH aliases, for example alpha_liu,Song-1")
    p.add_argument("--pattern", help="filter SSH aliases by substring")
    p.add_argument("--config", help="SSH config path, default ~/.ssh/config")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--timeout", type=int, default=8)
    p.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    p.add_argument("--allow-actions", action="store_true", help="allow stop actions even when not bound to loopback")
    p.add_argument("--fake-lab", action="store_true", help="use built-in multi-server demo data")
    p.set_defaults(handler=ui.run)

    p = sub.add_parser("servers", help="probe SSH GPU servers or start the resource-details UI")
    p.add_argument("--hosts", help="comma-separated SSH aliases, for example alpha_liu,Song-1")
    p.add_argument("--pattern", help="filter SSH aliases by substring")
    p.add_argument("--config", help="SSH config path, default ~/.ssh/config")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--timeout", type=int, default=8)
    p.add_argument("--json", action="store_true", help="probe once and print JSON instead of starting web")
    p.add_argument("--allow-actions", action="store_true", help="allow stop actions even when not bound to loopback")
    p.add_argument("--fake-lab", action="store_true", help="use built-in multi-server demo data")
    p.set_defaults(handler=servers.run_dashboard)
    servers_sub = p.add_subparsers(dest="servers_command")

    p_list = servers_sub.add_parser("list", help="list SSH hosts from config")
    p_list.add_argument("--hosts", help="comma-separated SSH aliases")
    p_list.add_argument("--pattern", help="filter SSH aliases by substring")
    p_list.add_argument("--config", help="SSH config path, default ~/.ssh/config")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(handler=servers.run_list)

    p_probe = servers_sub.add_parser("probe", help="probe one SSH host or all selected hosts")
    p_probe.add_argument("alias", nargs="?")
    p_probe.add_argument("--all", action="store_true")
    p_probe.add_argument("--hosts", help="comma-separated SSH aliases")
    p_probe.add_argument("--pattern", help="filter SSH aliases by substring")
    p_probe.add_argument("--config", help="SSH config path, default ~/.ssh/config")
    p_probe.add_argument("--timeout", type=int, default=8)
    p_probe.add_argument("--json", action="store_true")
    p_probe.add_argument("--fake-lab", action="store_true", help="use built-in multi-server demo data")
    p_probe.set_defaults(handler=servers.run_probe)

    p_import = servers_sub.add_parser("import-ssh", help="save SSH hosts into ~/.labgpu/config.toml")
    p_import.add_argument("--hosts", help="comma-separated SSH aliases")
    p_import.add_argument("--pattern", help="filter SSH aliases by substring")
    p_import.add_argument("--tags", help="comma-separated tags to attach, for example A100,training")
    p_import.add_argument("--config", help="SSH config path, default ~/.ssh/config")
    p_import.add_argument("--json", action="store_true")
    p_import.set_defaults(handler=servers.run_import_ssh)

    return parser


if __name__ == "__main__":
    raise SystemExit(main())
