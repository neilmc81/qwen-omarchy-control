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

from . import discovery, policy, triage
from .desktop import DesktopController, DesktopError
from .mcp import serve


def cmd_triage(args) -> int:
    if args.action == "status":
        cfg = triage.load_config()
        print(json.dumps({
            "mode": cfg.get("mode"),
            "model": cfg.get("model"),
            "minConfidence": cfg.get("minConfidence"),
            "destructiveConfirm": cfg.get("destructiveConfirm"),
            "clearFloor": cfg.get("clearFloor"),
            "config": str(triage.CONFIG_FILE),
            "log": str(triage.LOG_FILE),
        }, indent=2))
        if cfg.get("mode") == "off":
            print("\nTriage is OFF: launch_agent behaves exactly as before.",
                  file=sys.stderr)
        return 0
    if args.action == "set":
        if args.mode not in triage.MODES:
            print(f"mode must be one of {', '.join(triage.MODES)}", file=sys.stderr)
            return 2
        cfg = triage.load_config()
        cfg["mode"] = args.mode
        triage.save_config(cfg)
        print(f"triage mode = {args.mode}")
        if args.mode != "off":
            try:
                triage._read_key(cfg)
            except triage.TriageError as exc:
                print(f"warning: {exc}", file=sys.stderr)
        return 0
    if args.action in ("review", "stats"):
        rows = triage.read_log(limit=args.limit)
        if args.action == "stats":
            print(json.dumps(triage.summarize(rows), indent=2))
            return 0
        for row in rows:
            print(f"{row.get('ts','')}  {str(row.get('verdict')):<8} "
                  f"route={str(row.get('route')):<16} "
                  f"conf={float(row.get('confidence') or 0):.2f} "
                  f"destr={float(row.get('destructive') or 0):.2f} "
                  f"clear={float(row.get('clear') or 0):.2f}  "
                  f"{row.get('reason','')}")
            if args.verbose and row.get("prompt"):
                print(f"    prompt: {row['prompt'][:160]}")
        if not rows:
            print(f"no triage entries yet ({triage.LOG_FILE})")
        return 0
    print("unknown triage action", file=sys.stderr)
    return 2


def cmd_audit(args) -> int:
    from . import audit
    rows = audit.read_log(limit=args.limit)
    if args.action == "stats":
        print(json.dumps(audit.summarize(rows), indent=2))
        return 0
    for row in rows:
        print(f"{row.get('ts','')}  {str(row.get('outcome')):<11} "
              f"app={str(row.get('app') or 'unknown'):<18} "
              f"{(row.get('goal') or '')[:60]}")
        if args.verbose:
            print(f"    reason: {row.get('reason','')}")
    if not rows:
        print(f"no GUI actions recorded yet ({audit.LOG_FILE})")
    return 0


def cmd_actions(args) -> int:
    from . import vision
    try:
        result = vision.describe_actions(args.window)
    except vision.VisionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"{(result.get('window_title') or 'window')}: "
          f"{len(result['actions'])} action(s)")
    for action in result["actions"]:
        marker = "->" if action["id"] == result.get("top_action") else "  "
        print(f"  {marker} {action['role']:<12} {action['label']}")
    return 0


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

    triage_p = sub.add_parser("triage", help="pre-dispatch agent-triage control")
    triage_sub = triage_p.add_subparsers(dest="action", required=True)
    triage_sub.add_parser("status", help="show mode and policy")
    set_p = triage_sub.add_parser("set", help="set mode (off|log|enforce)")
    set_p.add_argument("mode")
    review_p = triage_sub.add_parser("review", help="show recent decisions")
    review_p.add_argument("--limit", type=int, default=30)
    review_p.add_argument("-v", "--verbose", action="store_true")
    stats_p = triage_sub.add_parser("stats", help="verdict counts")
    stats_p.add_argument("--limit", type=int, default=1000)

    audit_p = sub.add_parser("audit", help="trajectory audit of GUI actions")
    audit_sub = audit_p.add_subparsers(dest="action", required=True)
    audit_stats = audit_sub.add_parser("stats", help="success counts per app")
    audit_stats.add_argument("--limit", type=int, default=2000)
    audit_review = audit_sub.add_parser("review", help="show recent actions")
    audit_review.add_argument("--limit", type=int, default=30)
    audit_review.add_argument("-v", "--verbose", action="store_true")

    actions_p = sub.add_parser("actions", help="what can I do in a window?")
    actions_p.add_argument("--window", help="target window (pid or title substring)")

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
            "open_url", "type_text", "find_element", "describe_actions",
        ]).items()):
            rows.append(f"{op[0]:<38} {op[1]}")
        print("\n".join(rows))
        return 0

    if args.command == "mcp":
        return serve()

    if args.command == "triage":
        return cmd_triage(args)

    if args.command == "audit":
        return cmd_audit(args)

    if args.command == "actions":
        return cmd_actions(args)

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