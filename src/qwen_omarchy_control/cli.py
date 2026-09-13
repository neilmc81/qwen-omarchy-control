"""CLI entry point for the desktop controller (also used by tests/scripts).

Usage:
  desktop-control ops                  list available operations with their level
  desktop-control run <op> [--json] [kwargs...]
  desktop-control mcp                  run the stdio MCP server
  desktop-control config               print resolved config summary
"""

from __future__ import annotations

import argparse
import json
import sys

from . import discovery, policy
from .desktop import DesktopController, DesktopError
from .mcp import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="desktop-control")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("ops", help="list available operations")

    run_p = sub.add_parser("run", help="run one operation")
    run_p.add_argument("op")
    run_p.add_argument("kwargs", nargs="*", help="key=value pairs")
    run_p.add_argument("--json", action="store_true")

    sub.add_parser("mcp", help="serve MCP over stdio")
    sub.add_parser("config", help="print config summary")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "ops":
        rows = []
        for op in sorted(policy.classify_many([
            "get_active_window", "list_windows", "list_workspaces", "get_monitors",
            "switch_workspace", "focus_window", "launch_app", "set_volume",
            "volume_up", "volume_down", "mute_audio", "unmute_audio",
            "get_audio_status", "get_system_status",
            "move_active_window_to_workspace", "close_active_window",
            "open_url", "type_text",
        ]).items()):
            rows.append(f"{op[0]:<38} {op[1]}")
        print("\n".join(rows))
        return 0

    if args.command == "mcp":
        return serve()

    if args.command == "config":
        apps = discovery.all_apps()
        print(json.dumps({
            "module_dir": __file__,
            "apps_found": len(apps),
            "sample": sorted(a.id for a in apps)[:12],
        }, indent=2))
        return 0

    if args.command == "run":
        ctrl = DesktopController()
        kwargs = {}
        for item in args.kwargs:
            if "=" not in item:
                print(f"bad kwarg: {item!r}", file=sys.stderr)
                return 2
            k, v = item.split("=", 1)
            kwargs[k] = v if not v.lstrip("-").isdigit() else int(v)
        try:
            result = ctrl.execute(args.op, **kwargs)
        except (DesktopError, policy.PolicyError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if isinstance(result, dict):
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(result)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())